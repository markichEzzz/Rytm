from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import override_settings, RequestFactory
from django.contrib import admin
from django.utils import timezone
from rest_framework.test import APITestCase
from events.models import Event, Organizer
from ingestion.models import EventCandidate, SourceAccount, RawSourceItem
from ingestion.services import approve_candidate, review_candidate
from ingestion.admin import EventCandidateAdmin
from . import tests as auth_fixtures

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class EventOwnershipTests(APITestCase):
    payload = auth_fixtures.AccountTests.payload
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='owner', email='PRIVATE_OWNER@example.com')
        self.other = User.objects.create_user(username='other', email='PRIVATE_OTHER@example.com')
        self.reviewer = User.objects.create_superuser('reviewer', 'staff@example.com', None)
        self.client.force_authenticate(self.user)
        self.user.profile.display_name = 'Owner'; self.user.profile.save()
    def submit(self, **overrides):
        result = self.client.post('/api/events/', {**self.payload(), **overrides}, format='multipart')
        self.assertEqual(result.status_code, 201, result.data)
        return EventCandidate.objects.get(event_id=result.data['id'])
    def test_submission_chain_stores_request_user_and_pending(self):
        candidate = self.submit()
        self.assertEqual(candidate.submitted_by_id, self.user.pk)
        self.assertEqual(candidate.event.submitted_by_id, self.user.pk)
        self.assertIsNone(candidate.raw_source_item_id)
        self.assertEqual((candidate.status, candidate.event.status), ('new', 'pending'))
    def test_spoofed_ownership_and_organizer_ignored(self):
        org = Organizer.objects.create(user=self.other, name='Other', contact_email='other@example.com', verified=True)
        candidate = self.submit(submitted_by=self.other.pk, user_id=self.other.pk, organizer=org.pk, status='approved')
        self.assertEqual(candidate.submitted_by_id, self.user.pk)
        self.assertIsNone(candidate.event.organizer_id)
        self.assertEqual(candidate.event.status, 'pending')
    def test_approval_reuses_event_id_and_preserves_owner_and_clock(self):
        candidate = self.submit(); original = candidate.event
        event, created = approve_candidate(candidate, self.reviewer)
        self.assertFalse(created); self.assertEqual(event.pk, original.pk)
        self.assertEqual(event.created_at, original.created_at)
        self.assertEqual(event.submitted_by_id, self.user.pk)
        self.assertEqual(event.description, original.description)
        self.assertEqual(event.status, 'approved'); self.assertEqual(Event.objects.count(), 1)
        candidate.refresh_from_db(); self.assertEqual(candidate.status, 'approved')
        repeated, again = approve_candidate(candidate, self.reviewer)
        self.assertEqual(repeated.pk, event.pk); self.assertFalse(again)
    def test_conflicting_trusted_owner_not_overwritten(self):
        candidate = self.submit()
        Event.objects.filter(pk=candidate.event_id).update(submitted_by=self.other)
        with self.assertRaises(ValidationError): approve_candidate(candidate, self.reviewer)
        candidate.event.refresh_from_db(); self.assertEqual(candidate.event.submitted_by_id, self.other.pk)
    def test_verified_organizer_autoapproved_and_owned(self):
        Organizer.objects.create(user=self.user, name='Verified', contact_email=self.user.email, verified=True)
        candidate = self.submit()
        self.assertEqual((candidate.status, candidate.event.status), ('approved', 'approved'))
        self.assertEqual(candidate.event.submitted_by_id, self.user.pk)
        self.assertFalse(self.user.is_staff)
    def test_rejection_and_legacy_event_admin_status_stay_consistent(self):
        candidate = self.submit(); review_candidate(candidate, self.reviewer, 'rejected')
        candidate.event.refresh_from_db(); self.assertEqual(candidate.event.status, 'rejected')
        review_candidate(candidate, self.reviewer, 'new')
        candidate.event.refresh_from_db(); self.assertEqual(candidate.event.status, 'pending')
        candidate.event.status='approved'; candidate.event.save()
        candidate.refresh_from_db(); self.assertEqual(candidate.status, 'approved')
    def test_candidate_owner_visible_readonly_in_admin(self):
        candidate = self.submit(); model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        request = RequestFactory().get('/'); request.user=self.reviewer
        self.assertIn('submitted_by', model_admin.get_readonly_fields(request, candidate))
        self.assertEqual(model_admin.public_description(candidate), candidate.description)
        self.client.force_login(self.reviewer)
        response=self.client.get(f'/admin/ingestion/eventcandidate/{candidate.pk}/change/')
        self.assertEqual(response.status_code, 200)
    def test_private_submissions_only_current_user_no_internal_data(self):
        candidate = self.submit()
        self.client.force_authenticate(self.other); other=self.submit()
        response=self.client.get('/api/me/submissions/?user_id='+str(self.user.pk))
        self.assertEqual([row['id'] for row in response.data], [other.pk])
        self.assertNotIn('extraction_notes', response.data[0]); self.assertNotIn('email', str(response.data))
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/api/me/submissions/').data[0]['id'], candidate.pk)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/me/submissions/').status_code, 401)
        self.assertEqual(self.client.get('/api/me/events/').status_code, 401)
    def test_approved_public_event_id_and_no_duplicate_submission_rows(self):
        candidate=self.submit(); approve_candidate(candidate, self.reviewer)
        rows=self.client.get('/api/me/submissions/').data
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['public_event_id'], candidate.event_id)
        self.assertEqual(rows[0]['status'], 'approved')
    def test_owned_approved_upcoming_and_past_filters(self):
        candidate=self.submit(); event,_=approve_candidate(candidate,self.reviewer)
        self.assertEqual([e['id'] for e in self.client.get('/api/me/events/?period=upcoming').data], [event.pk])
        Event.objects.filter(pk=event.pk).update(start_time=timezone.now()-timedelta(days=2), end_time=None)
        self.assertEqual(self.client.get('/api/me/events/?period=upcoming').data, [])
        self.assertEqual([e['id'] for e in self.client.get('/api/me/events/?period=past').data], [event.pk])
    def test_public_user_events_exclude_pending_rejected_and_other_owners(self):
        candidate=self.submit(); self.assertEqual(self.client.get(f'/api/users/{self.user.pk}/events/').data, [])
        event,_=approve_candidate(candidate,self.reviewer)
        response=self.client.get(f'/api/users/{self.user.pk}/events/')
        self.assertEqual([r['id'] for r in response.data], [event.pk])
        self.assertEqual(self.client.get(f'/api/users/{self.other.pk}/events/').data, [])
        event.status='rejected'; event.save()
        self.assertEqual(self.client.get(f'/api/users/{self.user.pk}/events/').data, [])
    def test_public_organizer_only_safe_user_fields(self):
        candidate=self.submit(); event,_=approve_candidate(candidate,self.reviewer)
        self.client.force_authenticate(None)
        data=self.client.get(f'/api/events/{event.pk}/').data
        self.assertEqual(set(data['organizer']), {'id','display_name','avatar_url','reputation_score','reputation_label','verified_organizer'})
        self.assertEqual(data['organizer']['id'], self.user.pk)
        self.assertNotIn('PRIVATE_OWNER', str(data))
    def test_machine_candidate_null_owner_survives_approval(self):
        source=SourceAccount.objects.create(username='machine')
        raw=RawSourceItem.objects.create(source_account=source,external_id='one',source_url='https://example.com/post')
        candidate=EventCandidate.objects.create(raw_source_item=raw,title='Machine concert',
            start_time=timezone.now()+timedelta(days=8),address='Lviv Center',latitude=49.84,longitude=24.03,
            category='concert',public_photo='owned.jpg')
        event,_=approve_candidate(candidate,self.reviewer)
        self.assertIsNone(event.submitted_by_id); self.assertIsNone(candidate.submitted_by_id)
        self.assertIsNone(self.client.get(f'/api/events/{event.pk}/').data['organizer'])
    def test_historical_owned_and_null_events_need_no_fake_candidate(self):
        from django.contrib.gis.geos import Point
        event=Event.objects.create(title='Historical',category='concert',description='History',start_time=timezone.now()+timedelta(days=3),location=Point(24.03,49.84),address='Lviv',status='approved',submitted_by=self.user)
        rows=self.client.get('/api/me/submissions/').data
        self.assertEqual(rows[0]['kind'],'event'); self.assertEqual(rows[0]['id'],event.pk)
        event.submitted_by=None;event.save()
        self.assertEqual(self.client.get('/api/me/events/').data,[])
        self.assertIsNone(self.client.get(f'/api/events/{event.pk}/').data['organizer'])
    def test_backfill_uses_only_explicit_linked_event_owner(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        candidate=self.submit()
        EventCandidate.objects.filter(pk=candidate.pk).update(submitted_by=None)
        unknown=EventCandidate.objects.create(title='Unknown owner')
        trusted=EventCandidate.objects.create(title='Keep explicit owner',submitted_by=self.other)
        before=get_user_model().objects.count()
        migration=import_module('ingestion.migrations.0008_proven_candidate_owners')
        migration.forwards(apps,SimpleNamespace(connection=connection))
        candidate.refresh_from_db();unknown.refresh_from_db();trusted.refresh_from_db()
        self.assertEqual(candidate.submitted_by_id,self.user.pk)
        self.assertIsNone(unknown.submitted_by_id)
        self.assertEqual(trusted.submitted_by_id,self.other.pk)
        self.assertEqual(get_user_model().objects.count(),before)
    def test_owned_candidate_creates_new_event_with_same_owner(self):
        candidate=EventCandidate.objects.create(submitted_by=self.user,title='Owned draft',description='User text',
            start_time=timezone.now()+timedelta(days=9),address='Lviv Center',latitude=49.84,longitude=24.03,
            category='concert',public_photo='owned.jpg')
        event,created=approve_candidate(candidate,self.reviewer)
        self.assertTrue(created);self.assertEqual(event.submitted_by_id,self.user.pk)
        self.assertEqual(event.description,'User text')

    def test_admin_approval_reports_reused_event_as_updated(self):
        candidate = self.submit()
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        request = RequestFactory().post('/')
        request.user = self.reviewer
        with patch.object(model_admin, 'message_user') as message:
            model_admin.approve_selected(request, EventCandidate.objects.filter(pk=candidate.pk))
        self.assertTrue(any('1 candidates updated' in str(call.args[1]) for call in message.call_args_list))
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'approved')
        self.assertEqual(candidate.event.submitted_by_id, self.user.pk)
