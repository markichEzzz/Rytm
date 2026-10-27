from datetime import timedelta
from decimal import Decimal
from io import StringIO
import tempfile
from unittest.mock import patch

from django.contrib import admin
from django.contrib.gis.geos import Point
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, RequestFactory, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from events.models import Event
from .admin import EventCandidateAdmin, RawSourceItemAdmin
from .collectors import CollectedSourceItem
from .extraction import extract_event_candidate
from .models import EventCandidate, RawSourceItem, SourceAccount
from .services import (
    approve_candidate, build_candidate_description, find_possible_duplicate,
    review_candidate, store_collected_item,
)


class IngestionTests(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(
            MEDIA_ROOT=media.name,
            STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                      'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.reviewer = get_user_model().objects.create_superuser('reviewer', 'reviewer@example.com', None)
        self.source = SourceAccount.objects.create(username='festrepublic')
        self.raw = RawSourceItem.objects.create(
            source_account=self.source, external_id='post-1', source_url='https://example.com/post/1',
            raw_text='PRIVATE ORIGINAL CAPTION', raw_image_url='https://example.com/private.jpg',
            raw_metadata={'private': 'PRIVATE METADATA'},
        )

    def candidate(self, **overrides):
        values = dict(
            raw_source_item=self.raw, title='Jazz evening', description='PRIVATE EXTRACTION DRAFT',
            start_time=timezone.now() + timedelta(days=7), venue_name='Test venue',
            address='Lviv, Test Street 1', latitude=49.8397, longitude=24.0297,
            price=Decimal('120'), category='party', source_image_url='https://example.com/private.jpg',
            public_photo='candidate_photos/manually-uploaded.jpg', confidence=0.9,
        )
        values.update(overrides)
        return EventCandidate.objects.create(**values)

    def test_source_unique_case_insensitive_and_normalized(self):
        source = SourceAccount.objects.create(username=' @Another.Source ')
        self.assertEqual(source.username, 'another.source')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SourceAccount.objects.bulk_create([SourceAccount(username='FESTREPUBLIC')])

    def test_seed_idempotent_preserves_edits_and_disabled(self):
        self.source.enabled = False
        self.source.display_name = 'Edited name'
        self.source.save()
        call_command('seed_source_accounts', stdout=StringIO())
        call_command('seed_source_accounts', stdout=StringIO())
        self.assertEqual(SourceAccount.objects.count(), 21)
        self.source.refresh_from_db()
        self.assertFalse(self.source.enabled)
        self.assertEqual(self.source.display_name, 'Edited name')
        self.assertTrue(SourceAccount.objects.filter(username='les_kurbas_theatre').exists())
        self.assertTrue(SourceAccount.objects.filter(username='leskurbastheatre').exists())

    def test_raw_ingestion_idempotent_preserves_first_snapshot(self):
        item = CollectedSourceItem('post-1', 'https://example.com/changed', raw_text='changed')
        raw, created = store_collected_item(self.source, item)
        self.assertFalse(created)
        self.assertEqual(raw.raw_text, 'PRIVATE ORIGINAL CAPTION')
        raw, created = store_collected_item(self.source, CollectedSourceItem('post-2', 'https://example.com/2'))
        self.assertTrue(created)
        self.assertEqual(RawSourceItem.objects.count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RawSourceItem.objects.create(source_account=self.source, external_id='post-2', source_url='https://example.com/2')

    def test_disabled_source_cannot_ingest(self):
        self.source.enabled = False
        with self.assertRaisesMessage(ValidationError, 'disabled'):
            store_collected_item(self.source, CollectedSourceItem('new', 'https://example.com/new'))

    def test_stub_and_offline_command_never_publish(self):
        self.assertIsNone(extract_event_candidate(self.raw))
        call_command('ingest_test_event', stdout=StringIO())
        call_command('ingest_test_event', stdout=StringIO())
        self.assertEqual(EventCandidate.objects.count(), 1)
        self.assertEqual(RawSourceItem.objects.count(), 2)
        candidate = EventCandidate.objects.get()
        self.assertEqual(candidate.status, 'new')
        self.assertFalse(candidate.public_photo)
        self.assertEqual(Event.objects.count(), 0)

    def test_caption_draft_and_image_never_enter_description(self):
        text = build_candidate_description(self.candidate())
        self.assertIn('Jazz evening', text)
        self.assertIn('Test venue', text)
        self.assertIn('120.00', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('private.jpg', text)

    def test_missing_manual_photo_blocks_approval(self):
        candidate = self.candidate(public_photo='')
        with self.assertRaisesMessage(ValidationError, 'Upload a photo'):
            approve_candidate(candidate, self.reviewer)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'new')
        self.assertIsNone(candidate.event_id)
        self.assertEqual(Event.objects.count(), 0)

    def test_approval_idempotent_fact_mapping_and_no_notification(self):
        candidate = self.candidate()
        with patch('events.signals._send_approval_notification') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                event, created = approve_candidate(candidate, self.reviewer)
                repeated, repeated_created = approve_candidate(candidate, self.reviewer)
            notify.assert_not_called()
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated.pk, event.pk)
        self.assertEqual(Event.objects.count(), 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'approved')
        self.assertEqual(candidate.event_id, event.pk)
        self.assertEqual(candidate.reviewed_by, self.reviewer)
        self.assertIsNotNone(candidate.reviewed_at)
        self.assertEqual(event.status, 'approved')
        self.assertEqual(event.location.x, candidate.longitude)
        self.assertEqual(event.location.y, candidate.latitude)
        self.assertEqual(event.source_url, self.raw.source_url)
        self.assertEqual(event.photo.name, candidate.public_photo.name)
        self.assertEqual(event.description, build_candidate_description(candidate))
        response = APIClient().get(f'/api/events/{event.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {
            'id', 'title', 'description', 'category', 'price', 'price_status', 'start_time', 'end_time',
            'location', 'latitude', 'longitude', 'address', 'photo', 'source_url', 'organizer', 'status', 'venue',
        })
        self.assertNotIn('PRIVATE', str(response.data))
        self.assertNotIn('private.jpg', str(response.data))

    def test_candidate_source_url_overrides_raw_reference(self):
        event, _ = approve_candidate(self.candidate(source_url='https://example.com/event'), self.reviewer)
        self.assertEqual(event.source_url, 'https://example.com/event')

    def test_rejection_duplicate_and_return_to_review(self):
        candidate = self.candidate()
        for status in ('rejected', 'duplicate'):
            candidate = review_candidate(candidate, self.reviewer, status)
            self.assertEqual(candidate.status, status)
            with self.assertRaises(ValidationError):
                approve_candidate(candidate, self.reviewer)
            candidate = review_candidate(candidate, self.reviewer, 'new')
            self.assertIsNone(candidate.reviewed_at)
            self.assertIsNone(candidate.reviewed_by)
        self.assertFalse(Event.objects.exists())

    def test_published_candidate_cannot_be_rejected_or_republished_after_event_deleted(self):
        candidate = self.candidate()
        event, _ = approve_candidate(candidate, self.reviewer)
        with self.assertRaises(ValidationError):
            review_candidate(candidate, self.reviewer, 'rejected')
        event.delete()
        with self.assertRaises(ValidationError):
            approve_candidate(candidate, self.reviewer)
        self.assertFalse(Event.objects.exists())

    def test_duplicate_across_sources_normalized_and_flagged_without_deleting_raw(self):
        first = self.candidate()
        event, _ = approve_candidate(first, self.reviewer)
        other = SourceAccount.objects.create(username='other')
        raw = RawSourceItem.objects.create(source_account=other, external_id='repost', source_url='https://example.com/repost')
        candidate = self.candidate(raw_source_item=raw, title=' JAZZ evening! ',
                                   address='LVIV, Test Street 1.', start_time=first.start_time + timedelta(minutes=30))
        with self.assertRaisesMessage(ValidationError, 'Possible duplicate'):
            approve_candidate(candidate, self.reviewer)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'duplicate')
        self.assertIsNone(candidate.event_id)
        self.assertIn(f'Event #{event.pk}', candidate.extraction_notes)
        self.assertEqual(Event.objects.count(), 1)
        self.assertTrue(RawSourceItem.objects.filter(pk=raw.pk).exists())

    def test_fuzzy_duplicate_and_time_boundary(self):
        first = self.candidate(title='Saturday evening jazz concert')
        event, _ = approve_candidate(first, self.reviewer)
        second = self.candidate(title='Saturday evening jazz concerts', address='Lviv Test Streets 1', start_time=first.start_time)
        self.assertEqual(find_possible_duplicate(second), event)
        second.start_time += timedelta(minutes=30, seconds=1)
        self.assertIsNone(find_possible_duplicate(second))
        second.start_time = first.start_time
        second.address = 'Other location 99'
        self.assertIsNone(find_possible_duplicate(second))
        event.status = 'rejected'
        event.save()
        second.address = first.address
        self.assertIsNone(find_possible_duplicate(second))

    def test_invalid_facts_block_approval(self):
        for fields in ({'latitude': float('nan')}, {'longitude': 181}, {'confidence': float('nan')},
                       {'price': -1}, {'title': ' '}, {'category': 'unknown'},
                       {'start_time': timezone.now() - timedelta(days=1)},
                       {'end_time': timezone.now()}, {'source_url': 'https://example.com/' + 'x' * 210}):
            with self.subTest(fields=fields):
                candidate = self.candidate(**fields)
                with self.assertRaises(ValidationError):
                    approve_candidate(candidate, self.reviewer)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'new')
                self.assertIsNone(candidate.event_id)
        self.assertFalse(Event.objects.exists())

    def test_publication_rolls_back_if_candidate_link_save_fails(self):
        candidate = self.candidate()
        with patch.object(EventCandidate, 'save', side_effect=RuntimeError('link save failed')):
            with self.assertRaises(RuntimeError):
                approve_candidate(candidate, self.reviewer)
        self.assertFalse(Event.objects.exists())
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'new')

    def test_moderation_requires_staff_and_permissions(self):
        candidate = self.candidate()
        user = get_user_model().objects.create_user('ordinary')
        with self.assertRaises(PermissionDenied):
            approve_candidate(candidate, user)
        user.is_staff = True
        user.save()
        with self.assertRaises(PermissionDenied):
            approve_candidate(candidate, user)
        with self.assertRaises(PermissionDenied):
            review_candidate(candidate, user, 'rejected')
        self.assertFalse(Event.objects.exists())

    def test_admin_bulk_approval_feedback_and_immutable_raw_snapshot(self):
        valid = self.candidate()
        invalid = self.candidate(title='Different event', public_photo='')
        request = RequestFactory().get('/admin/')
        request.user = self.reviewer
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        with patch.object(model_admin, 'message_user') as feedback:
            model_admin.approve_selected(request, EventCandidate.objects.filter(pk__in=[valid.pk, invalid.pk]))
        valid.refresh_from_db()
        invalid.refresh_from_db()
        self.assertEqual(valid.status, 'approved')
        self.assertEqual(invalid.status, 'new')
        self.assertTrue(any(f'Candidate #{invalid.pk}' in str(call) for call in feedback.call_args_list))
        self.assertIn('title', model_admin.get_readonly_fields(request, valid))
        raw_admin = RawSourceItemAdmin(RawSourceItem, admin.site)
        self.assertIn('raw_text', raw_admin.get_readonly_fields(request, self.raw))

    def test_ingestion_has_no_public_routes(self):
        for path in ('source-accounts', 'raw-source-items', 'event-candidates', 'ingestion'):
            self.assertEqual(APIClient().get(f'/api/{path}/').status_code, 404)

    def test_public_feed_excludes_unreviewed_candidates_and_pending_events(self):
        candidate = self.candidate()
        pending = Event.objects.create(
            title='Pending public model', description='pending internal event', category='party',
            start_time=candidate.start_time, address=candidate.address,
            location=Point(24.0297, 49.8397),
            photo='pending.jpg', status='pending', source_url='https://example.com/pending',
        )
        response = APIClient().get('/api/events/')
        self.assertEqual(response.status_code, 200)
        rows = response.data.get('results', []) if isinstance(response.data, dict) else response.data
        self.assertEqual(rows, [])
        self.assertEqual(APIClient().get(f'/api/events/{pending.pk}/').status_code, 404)
        event, _ = approve_candidate(candidate, self.reviewer)
        response = APIClient().get('/api/events/')
        rows = response.data.get('results', []) if isinstance(response.data, dict) else response.data
        self.assertEqual([row['id'] for row in rows], [event.pk])
        self.assertNotIn('PRIVATE', str(response.data))


class ConcurrentApprovalTests(TransactionTestCase):
    def test_parallel_cross_account_approvals_create_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections

        reviewer = get_user_model().objects.create_superuser('concurrent-reviewer', 'reviewer@example.com', None)
        start = timezone.now() + timedelta(days=7)
        candidates = []
        for username in ('account_one', 'account_two'):
            source = SourceAccount.objects.create(username=username)
            raw = RawSourceItem.objects.create(source_account=source, external_id='same-event', source_url='https://example.com/event')
            candidates.append(EventCandidate.objects.create(
                raw_source_item=raw, title='Same concert', address='Same venue 1',
                start_time=start, latitude=49.83, longitude=24.03, category='party',
                public_photo='candidate_photos/owned.jpg',
            ))
        barrier = Barrier(2)

        def approve(pk):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=reviewer.pk)
                candidate = EventCandidate.objects.get(pk=pk)
                barrier.wait(timeout=10)
                try:
                    return approve_candidate(candidate, user)[1]
                except ValidationError:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(approve, [candidate.pk for candidate in candidates]))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(sorted(EventCandidate.objects.values_list('status', flat=True)), ['approved', 'duplicate'])
