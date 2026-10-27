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
