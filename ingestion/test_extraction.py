from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone as utc
from decimal import Decimal
from io import StringIO
import json
import os
from threading import Barrier
from unittest.mock import Mock, patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import close_old_connections
from django.test import TestCase, SimpleTestCase, TransactionTestCase, RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from events.models import Event
from .admin import RawSourceItemAdmin
from .extraction import extract_raw_source_item, extract_facts, configured_provider
from .extractors.contracts import ExtractionError, ExtractionResult, KYIV, map_category, validate_result
from .extractors.parsers import parse_times, parse_price, preprocess, localize
from .models import SourceAccount, RawSourceItem, EventCandidate

REFERENCE = datetime(2026, 10, 7, 12, 0, tzinfo=KYIV)
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=utc.utc)
CAPTION = '11 жовтня о 19:00 зустрічаємось у FESTrepublic.\nLatexfauna live.\nКонцерт гурту.\nКвитки від 700 грн.'


class ParserTests(SimpleTestCase):
    def test_ukrainian_named_numeric_dates(self):
        for text in ('11 жовтня о 19:00', '11.10 о 19:00', '11/10 19:00', '11.10.2026 19:00', '11.10.2026. 19:00'):
            with self.subTest(text=text):
                start, end, _, _ = parse_times(text, REFERENCE)
                self.assertEqual(start, datetime(2026, 10, 11, 19, 0, tzinfo=KYIV))
                self.assertIsNone(end)
                self.assertEqual(start.utcoffset(), timedelta(hours=3))

    def test_relative_dates_and_weekday(self):
        for text, day in (('сьогодні о 19:00', 7), ('завтра 19:30', 8), ("у п’ятницю з 18:00", 9)):
            start, _, _, _ = parse_times(preprocess(text)[0], REFERENCE)
            self.assertEqual(start.day, day)
            self.assertTrue(timezone.is_aware(start))
        reference_utc = datetime(2026, 10, 7, 22, 0, tzinfo=utc.utc)
        start, _, _, _ = parse_times('завтра 19:00', reference_utc)
        self.assertEqual(start.day, 9)  # publication is already Oct 8 locally

    def test_time_ranges_and_overnight(self):
        for text, hours in (('11 жовтня 18:00–22:00', 4), ('11 жовтня з 18:00 до 22:00', 4),
                            ('11 жовтня 22:00–02:00', 4)):
            start, end, _, _ = parse_times(text, REFERENCE)
            self.assertEqual(end - start, timedelta(hours=hours))

    def test_year_rollover_only_near_new_year(self):
        reference = datetime(2026, 12, 29, 12, tzinfo=KYIV)
        start, _, notes, _ = parse_times('3 січня 19:00', reference)
        self.assertEqual(start.year, 2027)
        self.assertTrue(notes)
        start, _, _, _ = parse_times('1 жовтня 19:00', REFERENCE)
        self.assertEqual(start.year, 2026)
        start, _, _, _ = parse_times('11 жовтня 2025 о 19:00', REFERENCE)
        self.assertEqual(start.year, 2025)

    def test_dst_gap_fold_and_winter_offset(self):
        for text in ('29.03.2026 03:30', '25.10.2026 03:30'):
            start, end, notes, _ = parse_times(text, REFERENCE)
            self.assertIsNone(start)
            self.assertIsNone(end)
            self.assertIn('DST', ' '.join(notes))
        start, _, _, _ = parse_times('11.11.2026 19:00', REFERENCE)
        self.assertEqual(start.utcoffset(), timedelta(hours=2))

    def test_invalid_and_multiple_dates_times(self):
        for text in ('31.02.2026 19:00', '11 жовтня 25:00'):
            with self.assertRaises(ExtractionError):
                parse_times(text, REFERENCE)
        for text in ('11 жовтня і 12 жовтня о 19:00', '11 жовтня двері 18:00 концерт 19:00', '11 жовтня'):
            self.assertIsNone(parse_times(text, REFERENCE)[0])

    def test_currency_patterns(self):
        for text, value in (('500 грн', '500'), ('500₴', '500'), ('від 700 грн', '700'),
                            ('700 UAH', '700'), ('1 500,50 грн', '1500.50'), ('free', '0'),
                            ('безкоштовно', '0'), ('вхід вільний', '0')):
            with self.subTest(text=text):
                price, _ = parse_price(text)
                self.assertEqual(price, Decimal(value))
        self.assertIn('Minimum', ' '.join(parse_price('від 700 грн')[1]))
        for text in ('Ціна невідома', '500 грн або 700 грн', 'вхід вільний, 500 грн'):
            self.assertIsNone(parse_price(text)[0])

    def test_negative_price_rejected_and_precision_not_misparsed(self):
        with self.assertRaises(ExtractionError):
            parse_price('-500 грн')
        self.assertIsNone(parse_price('500.123 грн')[0])

    def test_categories_only_existing_values(self):
        self.assertEqual(map_category('concert'), 'concert')
        self.assertEqual(map_category('exhibition'), 'museums')
        self.assertEqual(map_category('food'), 'restaurants')
        self.assertIsNone(map_category('other'))
        self.assertEqual(map_category('theatre'), 'performance')
        from .extractors.contracts import CATEGORIES
        self.assertEqual(CATEGORIES, {key for key, _ in Event.CATEGORY_CHOICES})

    def test_preprocessing_strips_urls_and_unicode_spaces(self):
        text, urls = preprocess('11 жовтня\u00a0о 19:00 https://example.com/01.12')
        self.assertNotIn('example.com', text)
        self.assertEqual(urls, ['https://example.com/01.12'])

    def test_strict_provider_contract(self):
        payload = asdict(ExtractionResult(True, title='Test', confidence=0.9))
        payload['start_time'] = '2026-10-11T19:00:00+03:00'
        payload['price'] = '700.00'
        payload['category'] = 'concert'
        result = ExtractionResult.from_mapping(payload)
        self.assertEqual(result.price, Decimal('700'))
        self.assertEqual(result.category, 'concert')
        for changes in ({'is_event': 'true'}, {'confidence': float('nan')}, {'confidence': 1.1},
                        {'price': '-1'}, {'price': 'NaN'}, {'price': '0.001'},
                        {'start_time': '2026-10-11T19:00:00'}, {'title': ['wrong']}, {'extra': 'secret'}):
            with self.subTest(changes=changes), self.assertRaises(ExtractionError):
                ExtractionResult.from_mapping({**payload, **changes})
        with self.assertRaises(ExtractionError):
            ExtractionResult.from_mapping({'is_event': False})


class ExtractionTests(TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'deterministic'})
        env.start()
        self.addCleanup(env.stop)
        self.source = SourceAccount.objects.create(username='festrepublic', display_name='Context hint')
        self.raw = RawSourceItem.objects.create(source_account=self.source, external_id='post-1',
            source_url='https://www.instagram.com/p/example/', raw_text=CAPTION,
            raw_image_url='https://example.com/private.jpg', published_at=REFERENCE,
            raw_metadata={'original': 'audit'})
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP in tests'))
        network.start()
        self.addCleanup(network.stop)

    def test_clear_event_candidate_facts_description_private(self):
        outcome = extract_raw_source_item(self.raw)
        self.assertEqual(outcome.action, 'created')
        candidate = outcome.candidate
        self.assertEqual(candidate.title, 'Latexfauna')
        self.assertEqual(candidate.start_time, datetime(2026, 10, 11, 19, tzinfo=KYIV))
        self.assertEqual(candidate.price, Decimal(700))
        self.assertEqual(candidate.venue_name, 'FESTrepublic')
        self.assertEqual(candidate.category, 'concert')
        self.assertEqual(candidate.status, 'new')
        self.assertEqual(candidate.source_url, self.raw.source_url)
        self.assertEqual(candidate.source_image_url, self.raw.raw_image_url)
        self.assertFalse(candidate.public_photo)
        self.assertIsNone(candidate.latitude)
        self.assertEqual(candidate.address, '')
        self.assertNotEqual(candidate.description, CAPTION)
        self.assertNotIn('Квитки від', candidate.description)
        self.assertFalse(Event.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'extracted')
        self.assertEqual(self.raw.raw_metadata['original'], 'audit')
        self.assertIn('image_text_status', self.raw.raw_metadata)
        response = APIClient().get('/api/events/')
        self.assertEqual(response.status_code, 200)
        rows = response.data.get('results', []) if isinstance(response.data, dict) else response.data
        self.assertEqual(rows, [])
        self.assertNotIn('private.jpg', str(response.data))

    def test_non_event_ignored_and_preserves_raw(self):
        self.raw.raw_text = 'Спробуйте нашу нову каву!'
        self.raw.save()
        outcome = extract_raw_source_item(self.raw)
        self.assertEqual(outcome.action, 'ignored')
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'ignored')
        self.assertEqual(self.raw.raw_text, 'Спробуйте нашу нову каву!')

    def test_past_event_ignored_invalid_date_failed(self):
        for text, action in (('1 жовтня 2026 о 19:00\nArtist live.', 'ignored'),
                             ('31.02.2026 о 19:00\nArtist live.', 'failed')):
            self.raw.raw_text = text
            self.raw.extraction_status = 'new'
            self.raw.save()
            outcome = extract_raw_source_item(self.raw)
            self.assertEqual(outcome.action, action)
        self.assertFalse(EventCandidate.objects.exists())

    def test_idempotence_preserves_admin_edits_all_statuses(self):
        first = extract_raw_source_item(self.raw).candidate
        first.title = 'Admin corrected title'
        first.description = 'Admin internal draft'
        first.save()
        for status in ('new', 'rejected', 'duplicate', 'approved'):
            first.status = status
            first.save()
            provider = Mock()
            outcome = extract_raw_source_item(self.raw, provider=provider)
            self.assertEqual(outcome.action, 'skipped')
            provider.extract.assert_not_called()
            first.refresh_from_db()
            self.assertEqual(first.title, 'Admin corrected title')
            self.assertEqual(first.description, 'Admin internal draft')
        self.assertEqual(EventCandidate.objects.count(), 1)

    def test_failed_provider_preserves_deterministic_candidate_and_safe_log(self):
        provider = Mock()
        provider.extract.side_effect = RuntimeError('API_KEY=SECRET entire-provider-response')
        with self.assertLogs('ingestion.extraction', level='WARNING') as logs:
            outcome = extract_raw_source_item(self.raw, provider=provider)
        self.assertEqual(outcome.action, 'created')
        self.assertNotIn('SECRET', str(logs.output))
        self.assertIn('deterministic fallback', outcome.result.extraction_notes)
        self.assertEqual(outcome.candidate.title, 'Latexfauna')
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'extracted')
        self.assertEqual(extract_raw_source_item(self.raw).action, 'skipped')

    def test_malformed_provider_output_falls_back(self):
        provider = Mock()
        provider.extract.return_value = {'is_event': True, 'confidence': 'high'}
        self.assertEqual(extract_raw_source_item(self.raw, provider=provider).action, 'created')
        self.assertEqual(EventCandidate.objects.count(), 1)

    def test_provider_boundary_explicit_facts_win_and_caption_not_copied(self):
        provider = Mock()
        provider.extract.return_value = ExtractionResult(True, title='Invented', description=CAPTION,
            start_time=datetime(2027, 1, 1, 19, tzinfo=KYIV), venue_name='Invented',
            price=Decimal(99), category='museums', confidence=0.9)
        outcome = extract_raw_source_item(self.raw, provider=provider)
        self.assertEqual(outcome.candidate.title, 'Latexfauna')
        self.assertEqual(outcome.candidate.price, Decimal(700))
        self.assertEqual(outcome.candidate.category, 'concert')
        self.assertNotEqual(outcome.candidate.description, CAPTION)
        context = provider.extract.call_args.args[1]
        self.assertEqual(context.source_username, 'festrepublic')
        self.assertEqual(context.source_display_name, 'Context hint')

    def test_provider_cannot_infer_unknown_price(self):
        self.raw.raw_text = '11 жовтня о 19:00\nArtist live.'
        provider = Mock()
        provider.extract.return_value = ExtractionResult(True, price=Decimal(500), confidence=0.9)
        self.assertIsNone(extract_facts(self.raw, provider=provider).price)

    def test_uncertain_missing_time_or_multiple_dates_low_confidence(self):
        for text in ('11 жовтня\nArtist live.', '11 жовтня і 12 жовтня 19:00\nArtist live.'):
            self.raw.raw_text = text
            self.raw.save()
            result = extract_facts(self.raw)
            self.assertTrue(result.is_event)
            self.assertIsNone(result.start_time)
            self.assertLess(result.confidence, 0.6)

    def test_free_event_and_missing_venue_not_inferred_from_username(self):
        self.raw.raw_text = '11 жовтня о 19:00\nArtist live.\nВхід вільний'
        self.raw.save()
        candidate = extract_raw_source_item(self.raw).candidate
        self.assertEqual(candidate.price, Decimal(0))
        self.assertEqual(candidate.venue_name, '')
        self.assertEqual(candidate.address, '')

    def test_dry_run_no_candidate_or_status_write(self):
        output = StringIO()
        call_command('extract_events', item_id=self.raw.pk, dry_run=True, stdout=output)
        self.assertIn('Latexfauna', output.getvalue())
        self.assertIn('Candidates created: 0', output.getvalue())
        self.assertIn('Dry-run candidates (not saved): 1', output.getvalue())
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'new')

    def test_failed_dry_run_keeps_status(self):
        self.raw.raw_text = '31.02.2026 о 19:00\nArtist live.'
        self.raw.save()
        with self.assertRaises(CommandError):
            call_command('extract_events', dry_run=True, stdout=StringIO())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'new')

    def test_batch_continues_after_failure_and_source_limit_filter(self):
        other = SourceAccount.objects.create(username='other')
        RawSourceItem.objects.create(source_account=other, external_id='other', source_url='https://example.com/post', raw_text=CAPTION, published_at=REFERENCE)
        bad = RawSourceItem.objects.create(source_account=self.source, external_id='bad', source_url='https://example.com/post',
                                           raw_text='31.02.2026 19:00\nArtist live.', published_at=REFERENCE)
        later = RawSourceItem.objects.create(source_account=self.source, external_id='later', source_url='https://example.com/post', raw_text=CAPTION.replace('Latexfauna', 'Other artist'), published_at=REFERENCE)
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command('extract_events', source='@FESTREPUBLIC', limit=3, stdout=output)
        self.assertIn('Processed: 3', output.getvalue())
        self.assertIn('Candidates created: 2', output.getvalue())
        self.assertIn('Failed: 1', output.getvalue())
        self.assertTrue(EventCandidate.objects.filter(raw_source_item=later).exists())
        self.assertFalse(EventCandidate.objects.filter(raw_source_item__source_account=other).exists())
        bad.refresh_from_db()
        self.assertEqual(bad.extraction_status, 'failed')

    def test_retry_failed_command(self):
        self.raw.extraction_status = 'failed'
        self.raw.save()
        call_command('extract_events', item_id=self.raw.pk, stdout=StringIO())
        self.assertFalse(EventCandidate.objects.exists())
        call_command('extract_events', item_id=self.raw.pk, retry_failed=True, stdout=StringIO())
        self.assertEqual(EventCandidate.objects.count(), 1)

    def test_candidate_and_status_transaction_rollback(self):
        original_save = RawSourceItem.save
        def fail_status(instance, *args, **kwargs):
            if instance.extraction_status == 'extracted':
                raise RuntimeError('status write failed')
            return original_save(instance, *args, **kwargs)
        with patch.object(RawSourceItem, 'save', fail_status):
            self.assertEqual(extract_raw_source_item(self.raw).action, 'failed')
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'failed')

    def test_unconfigured_provider_falls_back_without_key(self):
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'openai'}, clear=True):
            with self.assertLogs('ingestion.extraction', level='WARNING'):
                outcome = extract_raw_source_item(self.raw, provider=configured_provider())
        self.assertEqual(outcome.action, 'created')
        self.assertIn('deterministic fallback', outcome.result.extraction_notes)

    def test_admin_extraction_action_and_candidate_link(self):
        user = get_user_model().objects.create_superuser('reviewer', 'reviewer@example.com', None)
        request = RequestFactory().get('/admin/')
        request.user = user
        model_admin = RawSourceItemAdmin(RawSourceItem, admin.site)
        self.assertTrue(model_admin.has_extract_permission(request))
        with patch.object(model_admin, 'message_user'):
            model_admin.run_extraction(request, RawSourceItem.objects.filter(pk=self.raw.pk))
        candidate = EventCandidate.objects.get()
        self.assertIn('View candidates (1)', model_admin.candidate_link(self.raw))
        self.assertIn(f'raw_source_item__id__exact={self.raw.pk}', model_admin.candidate_link(self.raw))


class ConcurrentExtractionTests(TransactionTestCase):
    def test_parallel_extraction_creates_one_candidate(self):
        source = SourceAccount.objects.create(username='festrepublic')
        raw = RawSourceItem.objects.create(source_account=source, external_id='concurrent', source_url='https://example.com/post', raw_text='messy concert')
        barrier = Barrier(2)
        def extract(pk):
            close_old_connections()
            try:
                provider = Mock()
                def response(item, context):
                    barrier.wait(timeout=10)
                    return ExtractionResult(True, title='Same concert', start_time=timezone.now() + timedelta(days=7), confidence=0.8)
                provider.extract.side_effect = response
                return extract_raw_source_item(RawSourceItem.objects.select_related('source_account').get(pk=pk), provider=provider).action
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            actions = list(executor.map(extract, [raw.pk, raw.pk]))
        self.assertEqual(sorted(actions), ['created', 'skipped'])
        self.assertEqual(EventCandidate.objects.count(), 1)
