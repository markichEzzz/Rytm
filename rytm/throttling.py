"""Best-effort beta abuse limits; cache is process-local unless configured otherwise."""
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle


class AuthIPThrottle(SimpleRateThrottle):
    scope = 'account_auth'

    def get_cache_key(self, request, view):
        # Never let an authenticated request select another auth-rate bucket.
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class AdminLoginThrottle(AuthIPThrottle):
    scope = 'admin_login'


class WriteThrottle(UserRateThrottle):
    scope = 'api_write'

    def allow_request(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        return super().allow_request(request, view)


class SubmissionThrottle(WriteThrottle):
    scope = 'submission'


class ProfileWriteThrottle(WriteThrottle):
    scope = 'profile_write'
