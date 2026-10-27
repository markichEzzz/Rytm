from django.core.exceptions import ValidationError as ModelValidationError
from django.db.models import Q
from django.http import Http404
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import TourRoute, RouteType, Difficulty
from .distance import attach_distances
from .serializers import RouteSerializer, RouteDetailSerializer

class RouteFilters(serializers.Serializer):
    q = serializers.CharField(required=False, allow_blank=True, max_length=200)
    route_type = serializers.ChoiceField(choices=RouteType.choices, required=False)
    difficulty = serializers.ChoiceField(choices=Difficulty.choices, required=False)
    duration_min = serializers.IntegerField(min_value=0, max_value=2147483647, required=False)
    duration_max = serializers.IntegerField(min_value=0, max_value=2147483647, required=False)
    distance_min = serializers.FloatField(min_value=0, required=False)
    distance_max = serializers.FloatField(min_value=0, required=False)
    def validate(self, attrs):
        import math
        for prefix in ('duration', 'distance'):
            lo, hi = attrs.get(prefix+'_min'), attrs.get(prefix+'_max')
            if any(v is not None and not math.isfinite(v) for v in (lo, hi)) or (lo is not None and hi is not None and lo > hi):
                raise serializers.ValidationError('Діапазон фільтра некоректний.')
        return attrs

def public_routes(params=None, pk=None):
    queryset = TourRoute.objects.filter(is_published=True).prefetch_related('stops')
    if pk is not None: queryset = queryset.filter(pk=pk)
    filters = RouteFilters(data=params or {})
    filters.is_valid(raise_exception=True)
    data = filters.validated_data
    for field in ('route_type', 'difficulty'):
        if field in data: queryset = queryset.filter(**{field:data[field]})
    for suffix, lookup in [('min','gte'),('max','lte')]:
        if 'duration_'+suffix in data:
            queryset = queryset.filter(**{'estimated_duration_minutes__'+lookup:data['duration_'+suffix]})
    if data.get('q'):
        q=data['q'].strip()
        queryset=queryset.filter(Q(title__icontains=q)|Q(short_description__icontains=q)|Q(description__icontains=q)|Q(stops__title__icontains=q)|Q(stops__address__icontains=q)).distinct()
    routes=[]
    for route in queryset:
        try: route.validate_for_publication(list(route.stops.all()))
        except ModelValidationError: continue  # Never expose a broken record, even after an unsafe bulk DB edit.
        routes.append(route)
    attach_distances(routes)
    return [route for route in routes if
        ('distance_min' not in data or route.distance_meters >= data['distance_min']*1000) and
        ('distance_max' not in data or route.distance_meters <= data['distance_max']*1000)]

class RouteListView(APIView):
    permission_classes=[AllowAny]
    def get(self, request):
        return Response(RouteSerializer(public_routes(request.query_params), many=True, context={'request':request}).data)

class RouteDetailView(APIView):
    permission_classes=[AllowAny]
    def get(self, request, pk):
        routes=public_routes(pk=pk)
        if not routes: raise Http404
        return Response(RouteDetailSerializer(routes[0], context={'request':request}).data)
