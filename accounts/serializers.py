from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from .models import UserProfile

class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    display_name = serializers.CharField(max_length=100, allow_blank=True, required=False, default='')

    def validate_email(self, value):
        value = value.strip().casefold()
        if get_user_model().objects.filter(email__iexact=value).exists() or UserProfile.objects.filter(email_key=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate(self, attrs):
        user = get_user_model()(email=attrs['email'], username=attrs['email'].split('@')[0], first_name=attrs['display_name'])
        try:
            validate_password(attrs['password'], user)
        except DjangoValidationError as error:
            raise serializers.ValidationError({'password': error.messages})
        return attrs

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

class GoogleSerializer(serializers.Serializer):
    id_token = serializers.CharField(write_only=True, trim_whitespace=False, max_length=16384)

class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True, max_length=4096)

class PublicProfileSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user_id', read_only=True)
    reputation = serializers.SerializerMethodField()
    stats = serializers.SerializerMethodField()
    verified_organizer = serializers.SerializerMethodField()

    def summary(self, obj):
        from .reputation import reputation_summary
        cache = self.context.setdefault('_reputation', {})
        if obj.user_id not in cache:
            cache[obj.user_id] = reputation_summary(obj.user_id)
        return cache[obj.user_id]

    def get_reputation(self, obj):
        return self.summary(obj)['reputation']

    def get_stats(self, obj):
        stats = self.summary(obj)['stats']
        return {key: stats[key] for key in ('approved_events', 'confirmed_complaints')}

    def get_verified_organizer(self, obj):
        from .reputation import is_verified
        return is_verified(obj.user_id)

    class Meta:
        model = UserProfile
        fields = ['id', 'display_name', 'avatar_url', 'bio', 'reputation', 'stats', 'verified_organizer']
        read_only_fields = ['id', 'avatar_url']

class OwnProfileSerializer(PublicProfileSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    google_linked = serializers.SerializerMethodField()

    def get_google_linked(self, obj):
        return obj.user.identities.filter(provider='google').exists()

    def get_stats(self, obj):
        return self.summary(obj)['stats']

    class Meta(PublicProfileSerializer.Meta):
        fields = [*PublicProfileSerializer.Meta.fields, 'email', 'created_at', 'updated_at', 'google_linked']
        read_only_fields = ['id', 'email', 'avatar_url', 'created_at', 'updated_at']

    def to_internal_value(self, data):
        if set(data) - {'display_name', 'bio'}:
            raise serializers.ValidationError({'non_field_errors': ['Only display_name and bio can be changed.']})
        return super().to_internal_value(data)
