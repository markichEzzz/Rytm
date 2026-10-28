from rest_framework import serializers
from decimal import Decimal
import math
from django.utils import timezone
from django.contrib.gis.geos import GEOSGeometry, Point, GEOSException
from django.utils.dateparse import parse_datetime
from datetime import datetime
from .models import Event, Organizer
from .pricing import infer_price_status, normalize_price
from django.core.exceptions import ValidationError as DjangoValidationError

class OrganizerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organizer
        fields = '__all__'


class AwareDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, data):
        parsed = data if isinstance(data, datetime) else None
        if isinstance(data, str):
            try:
                parsed = parse_datetime(data)
            except ValueError:
                pass  # Let DRF report malformed timestamps.
        if parsed is not None and timezone.is_naive(parsed):
            raise serializers.ValidationError('Include a timezone offset, such as +03:00 or Z.')
        return super().to_internal_value(data)

class EventSerializer(serializers.ModelSerializer):
    organizer = serializers.SerializerMethodField()
    venue = serializers.SerializerMethodField()

    def get_organizer(self, event):
        # Public user identity comes from proven ownership, not contact email.
        if not event.submitted_by_id or not event.submitted_by.is_active:
            return None
        from django.core.exceptions import ObjectDoesNotExist
        try: profile = event.submitted_by.profile
        except ObjectDoesNotExist: return None
        from accounts.reputation import reputation_summary, is_verified
        cache = self.context.setdefault('_reputation', {})
        if event.submitted_by_id not in cache:
            cache[event.submitted_by_id] = reputation_summary(event.submitted_by_id)
            cache[event.submitted_by_id]['verified_organizer'] = is_verified(event.submitted_by_id)
        reputation = cache[event.submitted_by_id]['reputation']
        return {'id': event.submitted_by_id, 'display_name': profile.display_name, 'avatar_url': profile.public_avatar_url,
                'reputation_score': reputation['score'], 'reputation_label': reputation['label'],
                'verified_organizer': cache[event.submitted_by_id]['verified_organizer']}

    def to_representation(self, instance):
        data=super().to_representation(instance)
        request=self.context.get('request')
        if request is not None and request.user.is_authenticated and self.context.get('_include_interest'):
            from .interactions import interest_states
            if '_interest_by_event' not in self.context:
                self.context['_interest_by_event']=interest_states(request.user)
            data['user_interest']=self.context['_interest_by_event'].get(instance.pk,'neutral')
        return data

    def get_venue(self, event):
        candidate = getattr(event, 'ingestion_candidate', None)
        return candidate.venue_name if candidate and candidate.venue_name else event.address

    start_time = AwareDateTimeField()
    end_time = AwareDateTimeField(required=False, allow_null=True)
    # Витягуємо координати з PostGIS у прості числа для Android
    latitude = serializers.FloatField(source='location.y', read_only=True)
    longitude = serializers.FloatField(source='location.x', read_only=True)

    def validate_price(self, value):
        if value is not None and value < Decimal('0'):
            raise serializers.ValidationError('Price must be non-negative.')
        return value

    def validate_location(self, value):
        try:
            value = value if isinstance(value, Point) else GEOSGeometry(value)
        except (GEOSException, ValueError, TypeError):
            raise serializers.ValidationError('Location must be a WGS84 POINT(longitude latitude).')
        if (not isinstance(value, Point) or value.empty or value.hasz
                or value.srid not in (None, 4326)):
            raise serializers.ValidationError('Location must be a two-dimensional WGS84 point.')
        if (not math.isfinite(value.x) or not math.isfinite(value.y)
                or not -180 <= value.x <= 180 or not -90 <= value.y <= 90):
            raise serializers.ValidationError('Location must contain valid longitude and latitude.')
        value.srid = 4326
        return value

    def validate(self, attrs):
        if 'price' in attrs or 'price_status' in attrs or self.instance is None:
            price = attrs.get('price', getattr(self.instance, 'price', None))
            status = attrs.get('price_status', infer_price_status(price) if 'price' in attrs or self.instance is None else self.instance.price_status)
            try:
                attrs['price'] = normalize_price(status, price)
            except DjangoValidationError as error:
                raise serializers.ValidationError(error.message_dict)
            attrs['price_status'] = status
        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        if self.instance is None and start is not None and start <= timezone.now():
            raise serializers.ValidationError({'start_time': 'Start must be in the future.'})
        if end is not None and start is not None and end < start:
            raise serializers.ValidationError({'end_time': 'End must be at or after start.'})
        return attrs

    class Meta:
            model = Event
            fields = ['id', 'title', 'description', 'category', 'price', 'price_status',
                    'start_time', 'end_time', 'location', 'latitude', 'longitude', 
                    'address', 'venue', 'photo', 'source_url', 'organizer', 'status']
            read_only_fields = ['status', 'organizer']
