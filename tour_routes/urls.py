from django.urls import path
from .views import RouteListView, RouteDetailView, RouteSubmissionView
urlpatterns=[path("routes/submit/", RouteSubmissionView.as_view(), name="route-submit"),path('routes/',RouteListView.as_view(),name='route-list'),path('routes/<int:pk>/',RouteDetailView.as_view(),name='route-detail')]
