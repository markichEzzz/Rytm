import time
from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from rest_framework.exceptions import AuthenticationFailed, APIException
from rest_framework.serializers import EmailField

class GoogleNotConfigured(APIException):
    status_code = 503
    default_detail = 'Google sign-in is not configured.'

class BoundedGoogleRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 15
        return super().__call__(*args, **kwargs)

def verify_google_token(token):
    audience = settings.GOOGLE_OAUTH_CLIENT_ID
    if not audience:
        raise GoogleNotConfigured()
    try:
        claims = id_token.verify_oauth2_token(token, BoundedGoogleRequest(), audience=audience)
        # Defense in depth and clear requirements for mocked/alternative verifiers.
        if (claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
                or claims.get('aud') != audience or claims.get('exp', 0) <= time.time()
                or claims.get('email_verified') is not True
                or not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255):
            raise ValueError('Invalid claims')
        email = EmailField().run_validation(claims.get('email')).strip().casefold()
        return {**claims, 'email': email}
    except Exception:
        # Never return token contents, verification exception details, or network bodies.
        raise AuthenticationFailed('Google token verification failed.') from None
