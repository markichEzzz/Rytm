from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from events.models import Event
from .admin import EventCandidateAdmin
from .extraction import candidate_from_result, event_identity, extract_raw_source_item, store_batch
from .extraction import extract_facts
from .extractors.contracts import ExtractionBatch, ExtractionResult, partial_result_from_mapping
from .models import EventCandidate, RawSourceItem, SourceAccount
from .services import approve_candidate, candidate_completion_errors, validate_approval


class DraftFactsTests(SimpleTestCase):
    def setUp(self):
        self.raw = RawSourceItem(source_account=SourceAccount(username='channel', platform='telegram'),
                                 source_url='https://t.me/channel/123', raw_image_url='https://example.com/poster.jpg')
        self.event = ExtractionResult(True, title='Known event', category='concert', confidence=.8,
                                      start_time=timezone.now() + timedelta(days=10))

    def preview(self, events=(), unresolved=()):
        with patch('ingestion.deduplication.find_match', return_value=(None, None)):
            return store_batch(self.raw, ExtractionBatch(True, events=events, unresolved=unresolved), ['new'], dry_run=True)

    def test_missing_date_and_address_are_valid_private_draft_fields(self):
        event = replace(self.event, start_time=None)
        candidate = candidate_from_result(self.raw, event)
        candidate.full_clean(exclude=['raw_source_item'], validate_constraints=False)
        self.assertIsNone(candidate.start_time)
        self.assertEqual(candidate.address, '')
        self.assertEqual(candidate.status, 'new')
        self.assertEqual(candidate.source_url, self.raw.source_url)
        self.assertEqual(candidate.source_image_url, self.raw.raw_image_url)
        self.assertEqual(self.preview((event,)).ready, 1)

    def test_missing_title_with_known_date_is_an_editable_draft(self):
        outcome = self.preview((replace(self.event, title=None),))
        self.assertEqual(outcome.ready, 1)
        self.assertEqual(outcome.needs_completion, 1)
        self.assertIn('title', outcome.unresolved[0]['reasons'])

    def test_incomplete_identity_is_stable_and_distinguishes_events(self):
        first = replace(self.event, start_time=None)
        self.assertEqual(event_identity(first), event_identity(replace(first)))
        self.assertNotEqual(event_identity(first)[0], event_identity(replace(first, title='Other event'))[0])

    def test_mixed_batch_keeps_both_detections_and_deduplicates_mentions(self):
        incomplete = replace(self.event, title='Other event', start_time=None)
        outcome = self.preview((self.event, incomplete, incomplete))
        self.assertEqual(outcome.ready, 2)
        self.assertEqual(outcome.duplicates, 1)
        self.assertEqual(len(outcome.unresolved), 1)

    def test_schema_invalid_date_retains_other_valid_facts(self):
        entry = dict(title='Known event', start_time='not a date', venue_name='Named venue',
                     price='від 300 грн', category='concert', reason='Invalid date.',
                     reasons={'start_time': ['Invalid timestamp.']})
        outcome = self.preview(unresolved=(entry,))
        self.assertEqual(outcome.ready, 1)
        self.assertEqual(outcome.unresolved[0]['price'], '300')
        self.assertIsNone(outcome.unresolved[0]['start_time'])

    def test_historical_generic_and_unidentified_entries_are_not_invented(self):
        past = replace(self.event, start_time=timezone.now() - timedelta(days=1))
        self.assertEqual(self.preview((past,)).ready, 0)
        self.assertEqual(self.preview((ExtractionResult(True),)).ready, 0)
        self.assertEqual(self.preview(unresolved=({'reason': 'Inspect poster', 'reasons': {'event_group': ['Unknown event count.']}},)).ready, 0)

    def test_telegram_prices_already_normalize_without_free_fallback(self):
        from .extractors.hybrid import result_mapping
        for text, expected in [('від 300 грн', 300), ('від 100 грн', 100), ('Від 150 грн', 150)]:
            result = partial_result_from_mapping({'price': text})
            self.assertEqual(result.price, Decimal(expected))
            self.assertEqual(candidate_from_result(self.raw, result).price_status, 'paid')
            mapping = {**result_mapping(self.event), 'price': text}
            self.assertEqual(ExtractionResult.from_mapping(mapping).price, Decimal(expected))
        for value in (None, 'невідомо', '-1', 'NaN'):
            result = partial_result_from_mapping({'price': value})
            self.assertIsNone(result.price)
            self.assertEqual(candidate_from_result(self.raw, result).price_status, 'unknown')

    def test_admin_warning_lists_current_fields_and_escapes_ambiguity(self):
        candidate = EventCandidate(title='Event', extraction_notes='[Extraction ambiguity] ["<script>Check venue</script>"]')
        view = EventCandidateAdmin(EventCandidate, admin.site)
        warning = str(view.completion_warning(candidate))
        self.assertIn('Needs manual completion before approval', warning)
        for name in ('start_time', 'address', 'latitude', 'longitude', 'public_photo', 'Ambiguous'):
            self.assertIn(name, warning)
        self.assertNotIn('<script>', warning)
        self.assertIn('&lt;script&gt;', warning)

    def test_live_readiness_and_approval_remain_strict_after_completion(self):
        candidate = EventCandidate(title='Event')
        with self.assertRaises(ValidationError):
            validate_approval(candidate)
        candidate.start_time = self.event.start_time
        candidate.category = 'concert'
        candidate.address = 'Lviv address'
        candidate.latitude, candidate.longitude = 49.84, 24.03
        candidate.public_photo = 'owned-photo.jpg'
        self.assertFalse(candidate_completion_errors(candidate))
        validate_approval(candidate)
        candidate.extraction_notes = '[Extraction ambiguity] ["Confirm venue"]'
        with self.assertRaises(ValidationError):
            validate_approval(candidate)
        candidate.extraction_notes += '\n[Extraction confirmed] Moderator verified source ambiguities.'
        validate_approval(candidate)

    def test_invalid_date_draft_keeps_grounded_title_and_price_on_cached_retry(self):
        from . import test_multi_extraction as fixtures
        from .extractors.openai import OpenAIEventExtractionProvider
        from .test_hybrid import response
        entry, caption = fixtures.event(0)
        entry.update(start_time='invalid date', price='від 300 грн')
        entry['evidence']['price'] = [{'source': 'caption', 'quote': 'від 300 грн'}]
        self.raw.raw_text = caption + '\nвід 300 грн'
        self.raw.published_at = fixtures.NOW
        transport = Mock(return_value=response({'is_event': True, 'events': [entry], 'overall_notes': '', 'needs_review': True}))
        provider = OpenAIEventExtractionProvider(api_key='TEST_ONLY_KEY', model='test-model', transport=transport)
        with patch('django.utils.timezone.now', return_value=fixtures.NOW):
            for _ in range(2):
                batch = extract_facts(self.raw, provider=provider)
                self.assertEqual(batch.unresolved[0]['title'], 'Гурт 0')
                self.assertEqual(batch.unresolved[0]['price'], '300')
                self.assertIsNone(batch.unresolved[0]['start_time'])
                self.assertEqual(self.preview(batch.events, batch.unresolved).ready, 1)
        transport.assert_called_once()

    def test_poster_cross_group_date_stays_unknown_in_persistable_draft(self):
        from . import test_poster_layout as fixtures
        wrong = replace(fixtures.result(), start_time=fixtures.result('Гурт Б', 26, 20).start_time,
            evidence={**fixtures.result().evidence, 'start_time': [{'source': 'ocr', 'quote': '26 жовтня 20:00'}]})
        self.raw.raw_metadata = fixtures.raw_layout(fixtures.rows() + fixtures.rows('Гурт Б', 26, 20, 400)).raw_metadata
        self.raw.published_at = fixtures.NOW
        provider = Mock(extract=Mock(return_value=ExtractionBatch(True, events=(wrong, fixtures.result('Гурт Б', 26, 20)))))
        with patch('django.utils.timezone.now', return_value=fixtures.NOW):
            batch = extract_facts(self.raw, provider=provider)
            self.assertIsNone(batch.events[0].start_time)
            self.assertEqual(batch.events[1].start_time.hour, 20)
            preview = self.preview(batch.events, batch.unresolved)
        self.assertEqual(preview.ready, 2)
        self.assertEqual(len(preview.unresolved), 1)

    def test_admin_form_exposes_confirmation_for_new_and_preserves_approved_readonly(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = Mock(is_active=True, is_staff=True, is_superuser=True)
        view = EventCandidateAdmin(EventCandidate, admin.site)
        candidate = EventCandidate(title='Draft')
        form = view.get_form(request, candidate)
        self.assertIn('confirm_extraction_review', form.base_fields)
        candidate.status = 'approved'
        form = view.get_form(request, candidate)
        self.assertNotIn('confirm_extraction_review', form.base_fields)
        self.assertIn('confirm_extraction_review', view.get_readonly_fields(request, candidate))

    def test_admin_save_tolerates_minimal_form_without_confirming_ambiguities(self):
        from .services import extraction_warnings
        view = EventCandidateAdmin(EventCandidate, admin.site)
        request = SimpleNamespace(user=Mock())
        for cleaned in (None, {}, {'confirm_extraction_review': True}):
            with self.subTest(cleaned=cleaned):
                candidate = EventCandidate(title='Event', latitude=49.84, longitude=24.03,
                    extraction_notes='[Extraction ambiguity] ["Verify venue"]')
                form = SimpleNamespace(changed_data=['venue_name'])
                if cleaned is not None:
                    form.cleaned_data = cleaned
                with patch.object(admin.ModelAdmin, 'save_model') as save:
                    view.save_model(request, candidate, form, change=True)
                    save.assert_called_once_with(request, candidate, form, True)
                self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))
                self.assertIn('existing coordinates require manual verification', candidate.extraction_notes)
                self.assertEqual(bool(extraction_warnings(candidate)), not bool(cleaned))
                self.assertIn('start_time', candidate_completion_errors(candidate))

    def test_storage_flow_marks_saved_drafts_extracted_and_reuses_reviewed_identity(self):
        """Exercise real store decisions with mocked DB operations, not a transaction integration test."""
        rows = []
        self.raw.pk = 1
        self.raw.save = Mock()
        related = Mock()
        related.all.return_value = rows
        related.select_for_update.return_value.order_by.return_value = rows
        full_clean = EventCandidate.full_clean

        def clean(candidate, **kwargs):
            full_clean(candidate, exclude=[field.name for field in candidate._meta.fields if field.is_relation],
                       validate_unique=False, validate_constraints=False)

        def store(candidate):
            candidate.pk = len(rows) + 1
            rows.append(candidate)
            return candidate, True

        with ExitStack() as stack:
            from django.db import connections
            stack.enter_context(patch.object(RawSourceItem, 'candidates', new_callable=PropertyMock, return_value=related))
            stack.enter_context(patch('ingestion.extraction.transaction.atomic', side_effect=lambda: nullcontext()))
            stack.enter_context(patch('ingestion.extraction.EventCandidate.full_clean', autospec=True, side_effect=clean))
            stack.enter_context(patch('ingestion.models.EventSourceObservation.objects.filter')).return_value.select_related.return_value.first.return_value = None
            stack.enter_context(patch('ingestion.extraction.RawSourceItem.objects.select_for_update')).return_value.get.return_value = self.raw
            stack.enter_context(patch.object(connections['default'], 'cursor'))
            stack.enter_context(patch('ingestion.deduplication.find_match', return_value=(None, None)))
            writer = stack.enter_context(patch('ingestion.deduplication.store_observation', side_effect=store))
            stack.enter_context(patch('ingestion.extraction.enrich_missing_location', return_value=False))
            stack.enter_context(patch('ingestion.extraction.enrich_candidate_pricing', return_value=False))
            batch = ExtractionBatch(True, events=(self.event, replace(self.event, title='Other event', start_time=None)))
            outcome = store_batch(self.raw, batch, ['new', 'extracted'], dry_run=False)
            self.assertEqual(len(outcome.candidates), 2)
            self.assertTrue(outcome.unresolved[0]['persisted'])
            self.assertEqual(self.raw.extraction_status, 'extracted')
            rows[1].title = 'Moderator title'
            rows[1].status = 'rejected'
            outcome = store_batch(self.raw, batch, ['new', 'extracted'], dry_run=False)
            self.assertEqual(outcome.matched, 2)
            self.assertEqual(writer.call_count, 2)
            self.assertEqual(rows[1].title, 'Moderator title')
            self.assertEqual(rows[1].status, 'rejected')


class DraftPersistenceTests(TestCase):
    def setUp(self):
        self.source = SourceAccount.objects.create(username='channel', platform='telegram')
        self.raw = RawSourceItem.objects.create(source_account=self.source, external_id='123',
            source_url='https://t.me/channel/123', raw_image_url='https://example.com/poster.jpg')
        self.event = ExtractionResult(True, title='Known event', category='concert', confidence=.8,
                                      start_time=timezone.now() + timedelta(days=10))
        extractor = patch('ingestion.extraction.extract_facts')
        self.facts = extractor.start()
        self.addCleanup(extractor.stop)
        images = patch('ingestion.extraction.prepare_image_text')
        images.start()
        self.addCleanup(images.stop)

    def extract(self, events, **kwargs):
        self.facts.return_value = ExtractionBatch(True, events=tuple(events))
        return extract_raw_source_item(self.raw, **kwargs)

    def test_complete_detection_and_missing_date_both_persist_and_raw_completes(self):
        draft = replace(self.event, title='Other event', start_time=None)
        outcome = self.extract([self.event, draft])
        self.assertEqual(len(outcome.candidates), 2)
        self.assertEqual(outcome.candidates[1].title, 'Other event')
        self.assertIsNone(outcome.candidates[1].start_time)
        self.assertEqual(outcome.candidates[1].address, '')
        self.assertIsNone(outcome.candidates[1].price)
        self.assertEqual(outcome.candidates[1].price_status, 'unknown')
        self.assertTrue(outcome.unresolved[0]['persisted'])
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'extracted')
        self.assertFalse(Event.objects.exists())

    def test_repeated_runs_preserve_moderator_edits_and_reviewed_rows(self):
        candidate = self.extract([replace(self.event, start_time=None)]).candidate
        candidate.title = 'Moderator title'
        candidate.extraction_notes += '\n[Candidate facts] Moderator edited: title'
        candidate.save()
        self.extract([self.event])
        self.assertEqual(EventCandidate.objects.count(), 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title, 'Moderator title')
        for status in ('approved', 'rejected', 'duplicate'):
            candidate.status = status
            candidate.save()
            self.extract([self.event], reextract=True)
            candidate.refresh_from_db()
            self.assertEqual(candidate.status, status)
            self.assertEqual(candidate.title, 'Moderator title')
            self.assertEqual(EventCandidate.objects.count(), 1)

    def test_explicit_reextract_repairs_same_draft_without_changing_fingerprint(self):
        candidate = self.extract([replace(self.event, start_time=None)]).candidate
        fingerprint = candidate.source_fingerprint
        outcome = self.extract([self.event], reextract=True)
        self.assertEqual(outcome.repaired, 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.source_fingerprint, fingerprint)
        self.assertEqual(candidate.start_time, self.event.start_time)
        self.assertEqual(EventCandidate.objects.count(), 1)

    def test_dry_run_is_write_free(self):
        before = self.raw.raw_metadata.copy()
        outcome = self.extract([replace(self.event, start_time=None)], dry_run=True)
        self.assertEqual(outcome.ready, 1)
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'new')
        self.assertEqual(self.raw.raw_metadata, before)

    def test_draft_cannot_publish_but_moderator_can_complete_and_approve(self):
        candidate = self.extract([replace(self.event, start_time=None)]).candidate
        moderator = get_user_model().objects.create_superuser('draft-moderator', 'draft@example.com', None)
        with self.assertRaises(ValidationError):
            approve_candidate(candidate, moderator)
        self.assertFalse(Event.objects.exists())
        candidate.start_time = self.event.start_time
        candidate.address = 'Lviv address'
        candidate.latitude, candidate.longitude = 49.84, 24.03
        candidate.public_photo = 'owned-photo.jpg'
        candidate.save()
        event, created = approve_candidate(candidate, moderator)
        self.assertTrue(created)
        self.assertEqual(event.status, 'approved')

    def test_command_reports_saved_drafts_without_not_persisted_message(self):
        self.facts.return_value = ExtractionBatch(True, events=(replace(self.event, start_time=None),))
        output = StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=None):
            call_command('extract_events', platform='telegram', stdout=output)
        text = output.getvalue()
        self.assertIn('Candidates created: 1', text)
        self.assertIn('Draft candidates needing completion: 1', text)
        self.assertIn('Candidates ready for review: 0', text)
        self.assertIn('Saved private draft Candidate #', text)
        self.assertNotIn('Not persisted', text)
