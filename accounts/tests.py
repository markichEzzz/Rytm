from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import time
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from events.models import Organizer, Event
from .models import UserProfile, UserIdentity

@override_settings(GOOGLE_OAUTH_CLIENT_ID='test-web-client')
class AccountTests(APITestCase):
    password = 'UniqueLongPass!9462'
    def setUp(self):
        cache.clear()
        self.claims = {'sub': 'google-subject', 'aud': 'test-web-client', 'iss': 'https://accounts.google.com',
            'exp': time.time() + 3600, 'email_verified': True, 'email': 'google@example.com',
            'name': 'Google Name', 'picture': 'https://example.com/avatar.jpg'}
        verify = patch('accounts.google.id_token.verify_oauth2_token', return_value=self.claims)
        self.verify = verify.start(); self.addCleanup(verify.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No network in auth tests'))
        network.start(); self.addCleanup(network.stop)

    def register(self, **values):
        return self.client.post('/api/auth/register/', {'email': 'user@example.com',
            'password': self.password, 'display_name': 'Marko', **values}, format='json')
    def auth(self, tokens):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])
    def google(self):
        return self.client.post('/api/auth/google/', {'id_token': 'MOCK_ID_TOKEN'}, format='json')

    def test_register_hashes_password_and_creates_profile(self):
        result = self.register(); self.assertEqual(result.status_code, 201)
        user = get_user_model().objects.get(email='user@example.com')
        self.assertTrue(user.check_password(self.password)); self.assertNotEqual(user.password, self.password)
        self.assertEqual(user.profile.display_name, 'Marko')
        self.assertEqual(set(result.data), {'access', 'refresh'})

    def test_duplicate_email_case_insensitive(self):
        self.register(); self.assertEqual(self.register(email='USER@EXAMPLE.COM').status_code, 400)
        self.assertEqual(get_user_model().objects.count(), 1)
    def test_invalid_email(self):
        self.assertEqual(self.register(email='no-email').status_code, 400)
    def test_weak_password(self):
        self.assertEqual(self.register(password='password').status_code, 400)
    def test_password_login(self):
        self.register()
        result = self.client.post('/api/auth/login/', {'email': 'USER@example.com', 'password': self.password})
        self.assertEqual(result.status_code, 200); self.assertIn('refresh', result.data)
    def test_wrong_password(self):
        self.register()
        self.assertEqual(self.client.post('/api/auth/login/', {'email': 'user@example.com', 'password': 'wrong'}).status_code, 401)
    def test_admin_user_automatically_gets_profile(self):
        user = get_user_model().objects.create_user(username='legacy', email='legacy@example.com')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
    def test_own_profile_get(self):
        result = self.register(); self.auth(result.data)
        profile = self.client.get('/api/me/profile/')
        self.assertEqual(profile.data['email'], 'user@example.com'); self.assertEqual(profile.data['display_name'], 'Marko')
        self.assertNotIn('password', profile.data)
    def test_own_profile_patch(self):
        self.auth(self.register().data)
        result = self.client.patch('/api/me/profile/', {'display_name': 'New name', 'bio': 'About me'}, format='json')
        self.assertEqual(result.status_code, 200); self.assertEqual(result.data['bio'], 'About me')
    def test_unauthenticated_own_profile_rejected(self):
        self.assertEqual(self.client.get('/api/me/profile/').status_code, 401)
    def test_public_profile_hides_email_and_internal_state(self):
        self.register(); user = get_user_model().objects.get(email='user@example.com')
        result = self.client.get(f'/api/users/{user.pk}/profile/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.data), {'id', 'display_name', 'avatar_url', 'bio', 'city', 'reputation', 'stats', 'verified_organizer'})
        self.assertNotIn('user@example.com', str(result.data))
    def test_public_another_user_and_missing_user(self):
        self.auth(self.register().data)
        other = get_user_model().objects.create_user(username='another', email='other@example.com')
        self.assertEqual(self.client.get(f'/api/users/{other.pk}/profile/').status_code, 200)
        self.assertEqual(self.client.get('/api/users/999999/profile/').status_code, 404)
    def test_protected_fields_rejected(self):
        self.auth(self.register().data)
        for field in ('email', 'id', 'is_staff', 'is_superuser', 'verified', 'avatar_url'):
            with self.subTest(field=field):
                self.assertEqual(self.client.patch('/api/me/profile/', {field: True}, format='json').status_code, 400)
        user = get_user_model().objects.get(email='user@example.com')
        self.assertFalse(user.is_staff); self.assertFalse(user.is_superuser)
    def test_invalid_google_signature(self):
        self.verify.side_effect = ValueError('SECRET_TOKEN')
        result = self.google(); self.assertEqual(result.status_code, 401)
        self.assertNotIn('SECRET_TOKEN', str(result.data)); self.assertFalse(UserIdentity.objects.exists())
    def test_wrong_google_audience(self):
        self.claims['aud'] = 'wrong-client'; self.assertEqual(self.google().status_code, 401)
    def test_google_issuer_expiry_verified_email_required(self):
        original = deepcopy(self.claims)
        for field, value in [('iss', 'evil'), ('exp', 1), ('email_verified', False)]:
            with self.subTest(field=field):
                self.verify.return_value = {**original, field: value}
                self.assertEqual(self.google().status_code, 401)
    def test_valid_google_creates_user_profile_identity(self):
        result = self.google(); self.assertEqual(result.status_code, 200)
        user = get_user_model().objects.get(email='google@example.com')
        self.assertFalse(user.has_usable_password()); self.assertEqual(user.profile.display_name, 'Google Name')
        self.assertEqual(user.profile.avatar_url, 'https://example.com/avatar.jpg')
        self.assertEqual(UserIdentity.objects.get().user_id, user.pk)
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.kwargs['audience'], 'test-web-client')
    def test_repeated_google_reuses_user(self):
        self.google(); self.google()
        self.assertEqual(get_user_model().objects.count(), 1); self.assertEqual(UserIdentity.objects.count(), 1)
    def test_same_email_requires_existing_account_authentication(self):
        tokens = self.register(email='google@example.com').data
        self.assertEqual(self.google().status_code, 409)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.auth(tokens); self.assertEqual(self.google().status_code, 200)
        self.assertEqual(UserIdentity.objects.count(), 1)
        self.assertEqual(get_user_model().objects.count(), 1)
    def test_cannot_link_different_email_or_other_users_identity(self):
        self.google(); tokens = self.register().data; self.auth(tokens)
        self.assertEqual(self.google().status_code, 409)
    def test_logout_blacklists_refresh(self):
        tokens = self.register().data; self.auth(tokens)
        self.assertEqual(self.client.post('/api/auth/logout/', {'refresh': tokens['refresh']}).status_code, 204)
        self.client.credentials()
        self.assertEqual(self.client.post('/api/token/refresh/', {'refresh': tokens['refresh']}).status_code, 401)
    def test_cannot_logout_another_account(self):
        other = get_user_model().objects.create_user(username='other')
        refresh = str(RefreshToken.for_user(other))
        self.auth(self.register().data)
        self.assertEqual(self.client.post('/api/auth/logout/', {'refresh': refresh}).status_code, 401)
    @override_settings(GOOGLE_OAUTH_CLIENT_ID='')
    def test_google_unconfigured(self):
        self.assertEqual(self.google().status_code, 503)
        self.verify.assert_not_called()
    def payload(self):
        image = BytesIO(); Image.new('RGB', (2,2), 'white').save(image, format='JPEG')
        return {'title': 'Concert in Lviv', 'description': 'A live music concert in the city center.',
            'category': 'concert', 'price': '100', 'start_time': (timezone.now()+timedelta(days=2)).isoformat(),
            'location': 'POINT(24.03 49.84)', 'address': 'Lviv, Market Square',
            'photo': SimpleUploadedFile('event.jpg', image.getvalue(), content_type='image/jpeg')}
    def submit_pair(self, tokens):
        self.auth(tokens)
        with patch('events.signals.messaging.send'):
            # Use local file storage; no cloud upload in tests.
            result = self.client.post('/api/events/', self.payload(), format='multipart')
            self.assertEqual(result.status_code, 201, result.data)
            self.assertEqual(self.client.post('/api/events/', self.payload(), format='multipart').status_code, 429)
        return result
    @override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
    def test_password_user_submission_and_cooldown(self):
        result = self.submit_pair(self.register().data)
        self.assertEqual(result.data['status'], 'pending')
        self.assertEqual(Event.objects.get().submitted_by.email, 'user@example.com')
    @override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
    def test_google_user_submission_and_verified_organizer_behavior(self):
        tokens = self.google().data
        user = get_user_model().objects.get(email='google@example.com')
        Organizer.objects.create(user=user, name='Verified organizer', contact_email=user.email, verified=True)
        result = self.submit_pair(tokens)
        self.assertEqual(result.data['status'], 'approved')
        self.assertEqual(Event.objects.get().submitted_by_id, user.pk)

    def test_admin_created_duplicate_email_keeps_separate_profiles(self):
        first = get_user_model().objects.create_user(username='first', email='same@example.com')
        second = get_user_model().objects.create_user(username='second', email='SAME@example.com')
        self.assertNotEqual(first.profile.pk, second.profile.pk)
        self.assertIsNone(second.profile.email_key)
        self.assertEqual(self.register(email='same@example.com').status_code, 400)

    def test_inactive_google_account_cannot_sign_in(self):
        self.google()
        get_user_model().objects.filter(email='google@example.com').update(is_active=False)
        self.assertEqual(self.google().status_code, 401)

    def test_existing_profile_backfill_is_safe_idempotent_and_private(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        first = get_user_model().objects.create_user(username='first', email='same@example.com')
        second = get_user_model().objects.create_user(username='second', email='SAME@example.com')
        unique = get_user_model().objects.create_user(username='private@example.com', email='private@example.com')
        UserProfile.objects.all().delete()
        backfill = import_module('accounts.migrations.0002_existing_profiles').backfill
        backfill(apps, SimpleNamespace(connection=connection)); backfill(apps, SimpleNamespace(connection=connection))
        self.assertEqual(UserProfile.objects.count(), 3)
        self.assertIsNone(UserProfile.objects.get(user=first).email_key)
        self.assertIsNone(UserProfile.objects.get(user=second).email_key)
        self.assertEqual(UserProfile.objects.get(user=unique).email_key, 'private@example.com')
        self.assertEqual(UserProfile.objects.get(user=unique).display_name, '')

    @override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
    def test_linked_google_login_keeps_same_account_submission_cooldown(self):
        tokens = self.register(email='google@example.com').data
        self.auth(tokens)
        self.assertEqual(self.client.post('/api/events/', self.payload(), format='multipart').status_code, 201)
        linked = self.google()
        self.assertEqual(linked.status_code, 200)
        self.auth(linked.data)
        self.assertEqual(self.client.post('/api/events/', self.payload(), format='multipart').status_code, 429)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(UserIdentity.objects.get().user_id, Event.objects.get().submitted_by_id)

    @override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
    def test_new_google_identity_signs_up_and_submits_without_organizer(self):
        self.assertFalse(get_user_model().objects.filter(email=self.claims['email']).exists())
        tokens = self.google()
        self.assertEqual(tokens.status_code, 200)
        self.assertEqual(set(tokens.data), {'access', 'refresh'})
        user = get_user_model().objects.get(email=self.claims['email'])
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(UserIdentity.objects.get(provider='google', provider_subject=self.claims['sub']).user_id, user.pk)
        self.assertFalse(Organizer.objects.filter(user=user).exists())
        self.auth(tokens.data)
        # Client ownership and moderation assertions must never grant privileges.
        other = get_user_model().objects.create_user(username='other-owner')
        organizer = Organizer.objects.create(user=other, name='Other verified', contact_email='other@example.com', verified=True)
        payload = {**self.payload(), 'submitted_by': other.pk, 'user': other.pk, 'organizer': organizer.pk, 'status': 'approved'}
        result = self.client.post('/api/events/', payload, format='multipart')
        self.assertEqual(result.status_code, 201, result.data)
        event = Event.objects.get()
        self.assertEqual(event.submitted_by_id, user.pk)
        self.assertIsNone(event.organizer_id)
        self.assertEqual(event.status, 'pending')
        self.assertFalse(Organizer.objects.filter(user=user).exists())
        self.assertEqual(self.client.post('/api/events/', self.payload(), format='multipart').status_code, 429)

    def test_existing_password_email_google_requires_clear_explicit_linking(self):
        self.register(email=self.claims['email'])
        result = self.google()
        self.assertEqual(result.status_code, 409)
        self.assertIn('електронна адреса вже використовується', str(result.data['detail']))
        self.assertIn('у наявний акаунт', str(result.data['detail']))
        self.assertEqual(result.data['detail'].code, 'account_linking_required')
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(UserIdentity.objects.exists())

    def test_logged_out_cannot_submit(self):
        result = self.client.post('/api/events/', self.payload(), format='multipart')
        self.assertEqual(result.status_code, 401)
        self.assertFalse(Event.objects.exists())
