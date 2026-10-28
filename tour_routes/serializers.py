from rest_framework import serializers
from .models import TourRoute, TourRouteStop

class RouteStopSerializer(serializers.ModelSerializer):
    class Meta:
        model = TourRouteStop
        fields = ['id', 'order', 'title', 'description', 'latitude', 'longitude', 'address', 'image_url', 'recommended_stop_minutes']

class RouteSerializer(serializers.ModelSerializer):
    distance_meters = serializers.FloatField(read_only=True)
    stop_count = serializers.SerializerMethodField()
    distance_kind = serializers.ReadOnlyField(default='straight_line')
    def get_stop_count(self, obj): return len(obj.stops.all())
    class Meta:
        model = TourRoute
        fields = ['id', 'title', 'short_description', 'route_type', 'estimated_duration_minutes', 'difficulty', 'cover_image', 'stop_count', 'distance_meters', 'distance_kind']

class RouteDetailSerializer(RouteSerializer):
    stops = RouteStopSerializer(many=True, read_only=True)
    class Meta(RouteSerializer.Meta):
        fields = [*RouteSerializer.Meta.fields, 'description', 'stops']


class SubmittedStopSerializer(serializers.Serializer):
    order = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)

    def validate(self, attrs):
        import math
        if not all(math.isfinite(attrs[key]) for key in ('latitude', 'longitude')):
            raise serializers.ValidationError('Координати мають бути скінченними числами.')
        return attrs


class RouteSubmissionSerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField()
    estimated_duration_minutes = serializers.IntegerField(min_value=1, max_value=2147483647)
    stops = SubmittedStopSerializer(many=True, min_length=2, max_length=100)

    class Meta:
        model = TourRoute
        fields = ['title', 'description', 'route_type', 'difficulty', 'estimated_duration_minutes', 'stops']
        extra_kwargs = {'route_type': {'required': True}, 'difficulty': {'required': True}}

    def validate_stops(self, stops):
        if [stop['order'] for stop in stops] != list(range(1, len(stops) + 1)):
            raise serializers.ValidationError('Зупинки мають бути впорядковані послідовно від 1.')
        return stops

    def create(self, validated_data):
        from django.db import transaction
        with transaction.atomic():
            stops = validated_data.pop('stops')
            route = TourRoute.objects.create(**validated_data, submitted_by=self.context['request'].user, is_published=False)
            for stop in stops:
                TourRouteStop.objects.create(route=route, **stop)
            return route
