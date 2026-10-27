from django.urls import path
from .views import RouteListView, RouteDetailView
urlpatterns=[path('routes/',RouteListView.as_view(),name='route-list'),path('routes/<int:pk>/',RouteDetailView.as_view(),name='route-detail')]
