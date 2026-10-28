from unittest.mock import patch
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.contrib.gis.geos import Point
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from events.models import Event, EventReport, Organizer, UserEventInteraction
from ingestion.models import EventCandidate
from accounts.models import UserProfile, UserIdentity, DeletedAccountMedia
from accounts.deletion import purge_deleted_media

class AccountDeletionTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('delete-me', email='private@example.com', password='strong_password!')
        self.other = get_user_model().objects.create_user('keep-me', email='other@example.com')
        refresh = RefreshToken.for_user(self.user)
        self.refresh = str(refresh)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(refresh.access_token))
        self.storage = patch('accounts.deletion.default_storage.delete').start()
        self.addCleanup(patch.stopall)
    def delete(self, **data):
        return self.client.delete('/api/me/account/', data, format='json')
    def event(self, **data):
        return Event.objects.create(title='Public concert', description='Private free text', category='concert',
            start_time=timezone.now()+timedelta(days=1), location=Point(24.03,49.84),
            address='Lviv', photo='events_photos/test.jpg', submitted_by=self.user, **data)
    def test_authenticated_delete(self):
        self.assertEqual(self.delete().status_code,204)
        self.assertFalse(get_user_model().objects.filter(pk=self.user.pk).exists())
    def test_anonymous_rejected(self):
        self.client.credentials()
        self.assertEqual(self.delete().status_code,401)
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
    def test_cannot_delete_other_account(self):
        self.assertEqual(self.delete(user_id=self.other.pk).status_code,204)
        self.assertTrue(get_user_model().objects.filter(pk=self.other.pk).exists())
    def test_profile_removed_and_email_reservation_released(self):
        self.delete()
        self.assertFalse(UserProfile.objects.filter(user_id=self.user.pk).exists())
        replacement=get_user_model().objects.create_user('new',email=self.user.email)
        self.assertEqual(replacement.profile.email_key,self.user.email)
    def test_google_identity_removed(self):
        UserIdentity.objects.create(user=self.user,provider='google',provider_subject='subject',email=self.user.email)
        self.delete(); self.assertFalse(UserIdentity.objects.exists())
    def test_private_interactions_removed_other_users_preserved(self):
        event=self.event(status='approved')
        for user in [self.user,self.other]:
            UserEventInteraction.objects.create(user=user,event=event,interaction_type='interested')
        self.delete()
        self.assertEqual(list(UserEventInteraction.objects.values_list('user_id',flat=True)),[self.other.pk])
    def test_tokens_no_longer_usable(self):
        token=OutstandingToken.objects.get(user=self.user)
        LogEntry.objects.create(user=self.other, content_type=ContentType.objects.get_for_model(OutstandingToken),
            object_id=str(token.pk), object_repr='private token owner', action_flag=2)
        self.delete()
        self.assertFalse(LogEntry.objects.filter(object_repr='private token owner').exists())
        self.assertEqual(self.client.get('/api/me/profile/').status_code,401)
        self.client.credentials()
        self.assertEqual(self.client.post('/api/token/refresh/',{'refresh':self.refresh}).status_code,401)
        self.assertFalse(OutstandingToken.objects.filter(user=self.user).exists())
    def test_all_sessions_removed_only_for_this_account(self):
        keys=[]
        for user in [self.user,self.other]:
            session=SessionStore(); session['_auth_user_id']=str(user.pk);session.save();keys.append(session.session_key)
        self.delete()
        self.assertFalse(Session.objects.filter(session_key=keys[0]).exists())
        self.assertTrue(Session.objects.filter(session_key=keys[1]).exists())
    def test_public_event_detached_organizer_pii_removed(self):
        organizer=Organizer.objects.create(user=self.user,name='Private name',contact_email=self.user.email)
        event=self.event(status='approved',organizer=organizer,source_url='https://example.com/private')
        LogEntry.objects.create(user=self.other, content_type=ContentType.objects.get_for_model(Organizer),
            object_id=str(organizer.pk), object_repr=organizer.name, action_flag=2, change_message='private')
        self.delete();event.refresh_from_db()
        self.assertIsNone(event.submitted_by_id);self.assertIsNone(event.organizer_id)
        self.assertEqual(event.status,'approved');self.assertEqual(event.description,'');self.assertIsNone(event.source_url)
        self.assertFalse(Organizer.objects.filter(pk=organizer.pk).exists())
        self.assertFalse(LogEntry.objects.filter(object_repr='Private name').exists())
    def test_private_drafts_and_media_removed(self):
        event=self.event(status='pending')
        candidate=EventCandidate.objects.create(submitted_by=self.user,event=event,public_photo='candidate_photos/private.jpg')
        with self.captureOnCommitCallbacks(execute=True):self.delete()
        self.assertFalse(Event.objects.filter(pk=event.pk).exists());self.assertFalse(EventCandidate.objects.filter(pk=candidate.pk).exists())
        self.assertEqual(self.storage.call_count,2);self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_shared_public_image_not_deleted(self):
        event=self.event(status='approved')
        EventCandidate.objects.create(submitted_by=self.user,event=event,public_photo=event.photo.name)
        with self.captureOnCommitCallbacks(execute=True):self.delete()
        self.storage.assert_not_called();self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_storage_failure_retried_without_account_identity(self):
        self.event(status='rejected');self.storage.side_effect=OSError('provider offline')
        with self.captureOnCommitCallbacks(execute=True):self.assertEqual(self.delete().status_code,204)
        self.assertEqual(DeletedAccountMedia.objects.count(),1)
        self.storage.side_effect=None;purge_deleted_media();self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_report_anonymization(self):
        event=self.event(status='approved')
        report=EventReport.objects.create(event=event,reporter=self.user,reviewed_by=self.other,
            reason='other',description='Private report',moderator_note='Private note',status='confirmed')
        self.delete();report.refresh_from_db()
        self.assertIsNone(report.reporter_id);self.assertEqual(report.description,'');self.assertEqual(report.moderator_note,'')
        self.assertEqual(report.reason,'other');self.assertEqual(report.status,'confirmed');self.assertEqual(report.reviewed_by,self.other)
    def test_reviewer_links_cleared_other_submission_preserved(self):
        event=self.event(status='approved')
        report=EventReport.objects.create(event=event,reporter=self.other,reviewed_by=self.user,reason='other',moderator_note='private')
        candidate=EventCandidate.objects.create(submitted_by=self.other,reviewed_by=self.user,source_photo_authorized_by=self.user,title='Other draft')
        self.delete();report.refresh_from_db();candidate.refresh_from_db()
        self.assertIsNone(report.reviewed_by_id);self.assertEqual(report.moderator_note,'')
        self.assertEqual(candidate.submitted_by,self.other);self.assertIsNone(candidate.reviewed_by_id);self.assertIsNone(candidate.source_photo_authorized_by_id)
    def test_admin_logs_with_profile_identity_removed(self):
        ct=ContentType.objects.get_for_model(UserProfile)
        LogEntry.objects.create(user=self.other,content_type=ct,object_id=str(self.user.profile.pk),object_repr=self.user.email,action_flag=2,change_message='private')
        self.delete();self.assertFalse(LogEntry.objects.filter(content_type=ct).exists())
    def test_repeat_delete_is_safe(self):
        self.assertEqual(self.delete().status_code,204);self.assertEqual(self.delete().status_code,401)
    def test_departing_moderator_keeps_legacy_edit_protection_without_identity(self):
        from ingestion.provenance import moderator_edited_fields
        candidate=EventCandidate.objects.create(submitted_by=self.other,title='Protected')
        ct=ContentType.objects.get_for_model(EventCandidate)
        LogEntry.objects.create(user=self.user,content_type=ct,object_id=str(candidate.pk),object_repr='Protected',action_flag=2,
            change_message='[{"changed":{"fields":["title"]}}]')
        self.delete();candidate.refresh_from_db()
        self.assertIn('title',moderator_edited_fields(candidate));self.assertNotIn(self.user.email,candidate.extraction_notes)
    def test_transaction_rollback_keeps_account_and_media_on_database_error(self):
        with patch('django.contrib.auth.models.User.delete', side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):self.delete()
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists());self.storage.assert_not_called()
    def test_submitted_routes_erased_or_anonymized(self):
        from tour_routes.models import TourRoute, TourRouteStop
        draft = TourRoute.objects.create(title='Private walk', submitted_by=self.user, cover_image='route_covers/private.jpg')
        public = TourRoute.objects.create(title='Public walk', description='Private author text', short_description='Private summary',
            estimated_duration_minutes=30, submitted_by=self.user)
        for order in [1, 2]:
            TourRouteStop.objects.create(route=public, order=order, title=f'Stop {order}', description='Private stop text', latitude=49, longitude=24)
        public.is_published = True
        public.save()
        other = TourRoute.objects.create(title='Other draft', submitted_by=self.other)
        ct = ContentType.objects.get_for_model(TourRoute)
        LogEntry.objects.create(user=self.other, content_type=ct, object_id=str(draft.pk), object_repr='Private walk', action_flag=2)
        with self.captureOnCommitCallbacks(execute=True): self.delete()
        self.assertFalse(TourRoute.objects.filter(pk=draft.pk).exists())
        self.assertTrue(TourRoute.objects.filter(pk=other.pk, submitted_by=self.other).exists())
        public.refresh_from_db()
        self.assertTrue(public.is_published)
        self.assertIsNone(public.submitted_by_id)
        self.assertEqual(public.description, '')
        self.assertEqual(public.short_description, '')
        self.assertFalse(public.stops.exclude(description='').exists())
        self.assertFalse(LogEntry.objects.filter(content_type=ct).exists())
        self.storage.assert_called_once_with('route_covers/private.jpg')

    def test_current_user_relation_inventory(self):
        expected={'admin.logentry','accounts.userprofile','accounts.useridentity','events.organizer','events.event',
                  'events.eventreport','events.usereventinteraction','ingestion.eventcandidate','token_blacklist.outstandingtoken','tour_routes.tourroute'}
        actual={relation.related_model._meta.label_lower for relation in get_user_model()._meta.related_objects}
        self.assertEqual(actual,expected,'New User relation requires an erasure policy.')

class PublicReleasePagesTests(SimpleTestCase):
    def test_privacy(self):self.check_page('/privacy/','Політика конфіденційності')
    def test_terms(self):self.check_page('/terms/','Умови користування')
    def test_deletion(self):self.check_page('/account-deletion/','Видалення акаунта')
    def check_page(self,path,text):
        response=self.client.get(path)
        self.assertEqual(response.status_code,200);self.assertContains(response,text)
        self.assertContains(response,'viewport');self.assertNotContains(response,'<script')
    def test_health(self):self.assertEqual(self.client.get('/api/health/').json(),{'status':'ok'})

class ProductionSettingsTests(SimpleTestCase):
    def configuration(self, **changes):
        import os, runpy, secrets
        from pathlib import Path
        env={'ENVIRONMENT':'production','DEBUG':'false','SECRET_KEY':secrets.token_urlsafe(64),
             'ALLOWED_HOSTS':'api.rytm.example','DATABASE_URL':'postgres://user:unused@db/rytm',
             'CLOUDINARY_CLOUD_NAME':'test','CLOUDINARY_API_KEY':'unused','CLOUDINARY_API_SECRET':'unused',
             'GOOGLE_OAUTH_CLIENT_ID':'web-client.apps.googleusercontent.com'}
        env.update(changes)
        with patch.dict(os.environ,env,clear=True),patch('dotenv.load_dotenv'):
            return runpy.run_path(str(__import__('django.conf').conf.settings.BASE_DIR/'rytm/settings.py'))
    def test_production_secure_defaults(self):
        config=self.configuration()
        self.assertFalse(config['DEBUG']);self.assertTrue(config['SESSION_COOKIE_SECURE'])
        self.assertTrue(config['CSRF_COOKIE_SECURE']);self.assertTrue(config['SECURE_SSL_REDIRECT'])
        self.assertEqual(config['X_FRAME_OPTIONS'],'DENY');self.assertEqual(config['DATABASES']['default']['ENGINE'],'django.contrib.gis.db.backends.postgis')
        self.assertNotIn('SECURE_PROXY_SSL_HEADER',config)
    def rejects(self,**values):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):self.configuration(**values)
    def test_canonical_environment_names(self):
        config=self.configuration(DJANGO_SECRET_KEY='validation-only-secret-material-for-settings-tests-' * 2,
            DJANGO_DEBUG='false',DJANGO_ALLOWED_HOSTS='api.example.org')
        self.assertEqual(config['ALLOWED_HOSTS'],['api.example.org']);self.assertFalse(config['DEBUG'])
    def test_canonical_blank_secret_does_not_fall_back(self):self.rejects(DJANGO_SECRET_KEY='')
    def test_canonical_debug_wins(self):self.rejects(DJANGO_DEBUG='true')
    def test_unknown_environment_fails_clearly(self):self.rejects(ENVIRONMENT='prod')
    def test_optional_ingestion_credentials_not_required(self):
        config=self.configuration(OPENAI_API_KEY='',EVENT_EXTRACTION_PROVIDER='openai')
        self.assertIn('rest_framework',config['INSTALLED_APPS'])
    def test_production_manifest_storage(self):
        self.assertEqual(self.configuration()['STORAGES']['staticfiles']['BACKEND'],'whitenoise.storage.CompressedManifestStaticFilesStorage')
    def test_hsts_starts_disabled(self):
        config=self.configuration()
        self.assertEqual(config['SECURE_HSTS_SECONDS'],0);self.assertFalse(config['SECURE_HSTS_PRELOAD']);self.assertFalse(config['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
    def test_debug_rejected(self):self.rejects(DEBUG='true')
    def test_wildcard_host_rejected(self):self.rejects(ALLOWED_HOSTS='*')
    def test_missing_host_rejected(self):self.rejects(ALLOWED_HOSTS='')
    def test_missing_secret_rejected(self):self.rejects(SECRET_KEY='')
    def test_missing_database_rejected(self):self.rejects(DATABASE_URL='')
    def test_missing_cloudinary_rejected(self):self.rejects(CLOUDINARY_API_SECRET='')
    def test_disabled_https_rejected(self):self.rejects(SECURE_SSL_REDIRECT='false')
    def test_insecure_csrf_origin_rejected(self):self.rejects(CSRF_TRUSTED_ORIGINS='http://admin.example.org')
    def test_missing_google_audience_rejected(self):self.rejects(GOOGLE_OAUTH_CLIENT_ID='')
    def test_proxy_header_requires_explicit_opt_in(self):
        self.assertEqual(self.configuration(TRUST_PROXY_SSL_HEADER='true')['SECURE_PROXY_SSL_HEADER'],('HTTP_X_FORWARDED_PROTO','https'))


class MediaDeletionAcknowledgementTests(SimpleTestCase):
    @patch('accounts.storage.cloudinary.uploader.destroy',return_value={'result':'ok'})
    def test_success_invalidates_cdn(self,destroy):
        from accounts.storage import AccountMediaStorage
        AccountMediaStorage().delete('events_photos/private.jpg')
        destroy.assert_called_once_with('events_photos/private.jpg',invalidate=True,resource_type='image')
    @patch('accounts.storage.cloudinary.uploader.destroy',return_value={'result':'not found'})
    def test_already_absent_is_idempotent(self,destroy):
        from accounts.storage import AccountMediaStorage
        AccountMediaStorage().delete('events_photos/private.jpg')
    @patch('accounts.storage.cloudinary.uploader.destroy',return_value={'result':'unconfirmed'})
    def test_unacknowledged_delete_raises_for_outbox_retry(self,destroy):
        from accounts.storage import AccountMediaStorage
        with self.assertRaises(OSError):AccountMediaStorage().delete('events_photos/private.jpg')
