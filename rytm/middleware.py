"""Bound admin password attempts without changing Django's session/CSRF login."""
from django.http import JsonResponse
from django.urls import reverse
from .throttling import AdminLoginThrottle


class AdminLoginThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'POST' and request.path_info == reverse('admin:login'):
            throttle = AdminLoginThrottle()
            if not throttle.allow_request(request, None):
                response = JsonResponse({'detail': 'Too many login attempts. Try again later.'}, status=429)
                response['Retry-After'] = str(max(1, int(throttle.wait() or 60)))
                response['Cache-Control'] = 'no-store'
                return response
        return self.get_response(request)
