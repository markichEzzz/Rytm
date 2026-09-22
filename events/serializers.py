from rest_framework import serializers
from .models import Event, Organizer

class OrganizerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organizer
        fields = '__all__'

class EventSerializer(serializers.ModelSerializer):
    # Витягуємо координати з PostGIS у прості числа для Android
    latitude = serializers.FloatField(source='location.y', read_only=True)
    longitude = serializers.FloatField(source='location.x', read_only=True)

    class Meta:
            model = Event
            fields = ['id', 'title', 'description', 'category', 'price', 
                    'start_time', 'end_time', 'location', 'latitude', 'longitude', 
                    'address', 'photo', 'source_url', 'organizer', 'status']
            read_only_fields = ['status', 'organizer']