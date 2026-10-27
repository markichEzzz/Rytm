from django.urls import path
from .views import RegisterView, LoginView, GoogleView, LogoutView, OwnProfileView, PublicProfileView
from .event_views import OwnSubmissionsView, OwnEventsView, PublicUserEventsView
from .views import DeleteAccountView
urlpatterns = [
    path("me/account/", DeleteAccountView.as_view()),
    path("me/submissions/", OwnSubmissionsView.as_view()),
    path("me/events/", OwnEventsView.as_view()),
    path("users/<int:user_id>/events/", PublicUserEventsView.as_view()),
    path('auth/register/', RegisterView.as_view()),
    path('auth/login/', LoginView.as_view()),
    path('auth/google/', GoogleView.as_view()),
    path('auth/logout/', LogoutView.as_view()),
    path('me/profile/', OwnProfileView.as_view()),
    path('users/<int:user_id>/profile/', PublicProfileView.as_view()),
]
