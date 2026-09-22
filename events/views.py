from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from django.contrib.gis.geos import Polygon
from .models import Event
from .serializers import EventSerializer

class EventListCreateView(generics.ListCreateAPIView):
    serializer_class = EventSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = Event.objects.filter(status='approved')
        
        # Фільтр за категорією
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)
            
        # Географічний фільтр (межі карти)
        # Очікуваний формат: ?bbox=min_lon,min_lat,max_lon,max_lat
        bbox = self.request.query_params.get('bbox')
        if bbox:
            try:
                coords = [float(c) for c in bbox.split(',')]
                if len(coords) == 4:
                    # Створюємо багатокутник (квадрат) з координат
                    geom = Polygon.from_bbox(coords)
                    # Відбираємо події, які знаходяться всередині цього квадрата
                    queryset = queryset.filter(location__within=geom)
            except (ValueError, TypeError):
                pass # Якщо координати передані неправильно, ігноруємо фільтр

        return queryset

    def perform_create(self, serializer):
        serializer.save(status='pending')