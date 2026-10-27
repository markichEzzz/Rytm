from django.urls import path

from .interactions import EventInteractionView
from .reports import EventReportView
from .views import EventListCreateView, EventDetailView


urlpatterns = [
    path('events/<int:pk>/interaction/', EventInteractionView.as_view(), name='event-interaction'),
    path('events/<int:pk>/report/', EventReportView.as_view(), name='event-report'),
    path(
        'events/',
        EventListCreateView.as_view(),
        name='event-list'
    ),

    path(
        'events/<int:pk>/',
        EventDetailView.as_view(),
        name='event-detail'
    ),
]