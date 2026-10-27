from uuid import uuid4
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from rest_framework import generics, status
from rest_framework.exceptions import AuthenticationFailed, APIException
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from .google import verify_google_token
from .models import UserIdentity, UserProfile
from .serializers import (RegisterSerializer, LoginSerializer, GoogleSerializer, LogoutSerializer,
                          OwnProfileSerializer, PublicProfileSerializer)

class AccountConflict(APIException):
    status_code = 409
    default_detail = 'Ця електронна адреса вже використовується в Rytm. Увійдіть у наявний акаунт і прив’яжіть Google у профілі.'
    default_code = 'account_linking_required'


def credentials(user):
    if not user.is_active:
        raise AuthenticationFailed('Account is inactive.')
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}

class AuthView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'account_auth'

class RegisterView(AuthView):
    authentication_classes = []
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            with transaction.atomic():
                user = get_user_model().objects.create_user(username='rytm_' + uuid4().hex,
                    email=data['email'], password=data['password'])
                profile = user.profile
                if profile.email_key != data['email']:
                    raise IntegrityError('Email reservation conflict')
                profile.display_name = data['display_name']
                profile.save(update_fields=['display_name', 'updated_at'])
                result = credentials(user)
        except IntegrityError:
            return Response({'email': ['An account with this email already exists.']}, status=400)
        return Response(result, status=status.HTTP_201_CREATED)

class LoginView(AuthView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        matches = list(get_user_model().objects.filter(email__iexact=data['email'].strip())[:2])
        user = authenticate(request, username=matches[0].username, password=data['password']) if len(matches) == 1 else None
        if user is None:
            if not matches:
                make_password(data['password'])
            raise AuthenticationFailed('Email or password is incorrect.')
        return Response(credentials(user))

class GoogleView(AuthView):
    def post(self, request):
        serializer = GoogleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claims = verify_google_token(serializer.validated_data['id_token'])
        try:
            with transaction.atomic():
                identity = UserIdentity.objects.select_related('user').filter(provider='google', provider_subject=claims['sub']).first()
                if identity:
                    if request.user.is_authenticated and request.user.pk != identity.user_id:
                        raise AccountConflict()
                    return Response(credentials(identity.user))
                matches = list(get_user_model().objects.select_for_update().filter(email__iexact=claims['email'])[:2])
                if matches:
                    if len(matches) != 1 or not request.user.is_authenticated or request.user.pk != matches[0].pk:
                        raise AccountConflict()
                    user = matches[0]
                elif request.user.is_authenticated:
                    # Do not merge a Google identity carrying a different email.
                    raise AccountConflict()
                else:
                    user = get_user_model().objects.create_user(username='rytm_' + uuid4().hex,
                        email=claims['email'], password=None)
                    if user.profile.email_key != claims['email']:
                        raise AccountConflict()
                    profile = user.profile
                    name = claims.get('name')
                    profile.display_name = name[:100] if isinstance(name, str) else ''
                    picture = claims.get('picture')
                    if isinstance(picture, str) and len(picture) <= 2048:
                        try:
                            URLValidator(schemes=['https'])(picture)
                            profile.avatar_url = picture
                        except ValidationError:
                            pass
                    profile.save()
                UserIdentity.objects.create(user=user, provider='google', provider_subject=claims['sub'], email=claims['email'])
                result = credentials(user)
        except IntegrityError:
            raise AccountConflict() from None
        return Response(result)

class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token = RefreshToken(serializer.validated_data['refresh'])
            if str(token['user_id']) != str(request.user.pk):
                raise TokenError('Wrong owner')
            token.blacklist()
        except TokenError:
            raise AuthenticationFailed('Invalid refresh token.') from None
        return Response(status=204)

class OwnProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OwnProfileSerializer
    http_method_names = ['get', 'patch', 'head', 'options']
    def get_object(self):
        return get_object_or_404(UserProfile.objects.select_related('user'), user=self.request.user)

class PublicProfileView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = PublicProfileSerializer
    def get_object(self):
        return get_object_or_404(UserProfile, user_id=self.kwargs['user_id'], user__is_active=True)


class DeleteAccountView(APIView):
    permission_classes = [IsAuthenticated]
    def delete(self, request):
        from .deletion import delete_account
        # Deliberately ignore payload/query IDs: identity comes only from JWT.
        delete_account(request.user)
        return Response(status=204, headers={'Cache-Control': 'no-store'})


# SimpleJWT 5.5.1 raises User.DoesNotExist for deleted users on refresh.
# Convert it to a safe authentication response instead of a server error.
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenRefreshView
class ExistingAccountRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:
            return super().validate(attrs)
        except get_user_model().DoesNotExist:
            raise AuthenticationFailed('Account is unavailable.') from None

class ExistingAccountRefreshView(TokenRefreshView):
    serializer_class = ExistingAccountRefreshSerializer
