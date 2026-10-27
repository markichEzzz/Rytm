from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenObtainPairView

from accounts.views import ExistingAccountRefreshView
from .public_views import legal_page, health

urlpatterns = [
    path("api/health/", health),
    *[path(page + "/", legal_page, {"page": page}) for page in ("privacy", "terms", "account-deletion")],
    path('admin/', admin.site.urls),
    path('api/', include('events.urls')),
    path('api/', include('tour_routes.urls')),
    path('api/', include('accounts.urls')),
    
    # Ендпоінти для авторизації організаторів
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', ExistingAccountRefreshView.as_view(), name='token_refresh'),
]