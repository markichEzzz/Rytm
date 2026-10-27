from datetime import datetime
from decimal import Decimal
from io import StringIO
import json
import os
from unittest.mock import Mock, patch

from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase
from requests import Timeout

from events.models import Event
from .admin import RawSourceItemAdmin, EventCandidateAdmin
from .extraction import configured_provider, extract_raw_source_item
from .extractors.contracts import ExtractionResult, EVIDENCE_FIELDS, KYIV
from .extractors.hybrid import result_mapping
from .extractors.openai import OpenAIEventExtractionProvider
from .models import SourceAccount, RawSourceItem, EventCandidate

NOW = datetime(2026, 10, 7, 12, tzinfo=KYIV)
CAPTION = 'Концерт: ДахаБраха\n13 жовтня 19:00\nМісце: FESTrepublic\nКвитки від 700 грн'
OCR = 'ДахаБраха\n12 ЖОВТНЯ\n19:00\nFESTrepublic\nКВИТКИ ВІД 700 ГРН'


def suggestion(**updates):
    evidence = {name: [] for name in EVIDENCE_FIELDS}
    evidence.update({
        'title': [{'source': 'ocr', 'quote': 'ДахаБраха'}],
        'start_time': [{'source': 'ocr', 'quote': '12 ЖОВТНЯ\n19:00'}],
        'venue_name': [{'source': 'ocr', 'quote': 'FESTrepublic'}],
        'price': [{'source': 'ocr', 'quote': 'КВИТКИ ВІД 700 ГРН'}],
        'category': [{'source': 'caption', 'quote': 'Концерт: ДахаБраха'}],
    })
    result = result_mapping(ExtractionResult(True, title='ДахаБраха',
        start_time=datetime(2026, 10, 12, 19, tzinfo=KYIV), venue_name='FESTrepublic',
        price=Decimal('700'), category='concert', confidence=.98, event_count=1,
        needs_review=False, evidence=evidence))
    return {**result, **updates}


def response(mapping):
    body = {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps(mapping, ensure_ascii=False)}]}]}
    result = Mock(status_code=200)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [json.dumps(body).encode()]
    return result


class HybridTests(TestCase):
    def setUp(self):
        source = SourceAccount.objects.create(username='festrepublic', display_name='Unsafe venue hint')
        self.raw = RawSourceItem.objects.create(source_account=source, external_id='hybrid',
            source_url='https://www.instagram.com/p/hybrid/', raw_text=CAPTION,
            published_at=NOW, raw_image_url='https://private.cdninstagram.com/image?secret=PRIVATE',
            raw_metadata={'image_text': OCR, 'cookies': 'SECRET_COOKIE', 'browser_data': 'SECRET_BROWSER'})
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP in tests'))
        network.start()
        self.addCleanup(network.stop)
        self.transport = Mock(return_value=response(suggestion()))
        self.provider = OpenAIEventExtractionProvider(api_key='TEST_SECRET_KEY', model='configured-test-model', transport=self.transport)

    def run_result(self, mapping=None, **kwargs):
        if mapping is not None:
            self.transport.return_value = response(mapping)
        return extract_raw_source_item(self.raw, provider=self.provider, **kwargs)

    def test_ambiguous_dates_resolved_but_flagged_with_evidence(self):
        result = self.run_result()
        self.assertEqual(result.action, 'created')
        self.assertEqual(result.candidate.start_time.day, 12)
        self.assertEqual(result.candidate.title, 'ДахаБраха')
        self.assertEqual(result.candidate.status, 'new')
        self.assertLessEqual(result.result.confidence, .65)
        self.assertTrue(result.result.needs_review)
        self.assertIn('Multiple source dates', ' '.join(result.result.ambiguities))
        self.raw.refresh_from_db()
        audit = self.raw.raw_metadata['event_extraction']
        self.assertIsNone(audit['deterministic_result']['start_time'])
        self.assertEqual(audit['result']['evidence']['start_time'][0]['source'], 'ocr')
        self.assertIn('start_time', RawSourceItemAdmin(RawSourceItem, admin.site).semantic_extraction(self.raw))
        self.assertIn('start_time', EventCandidateAdmin(EventCandidate, admin.site).semantic_extraction(result.candidate))

    def test_unknown_field_stays_null_even_when_regex_has_venue(self):
        result = self.run_result(suggestion(venue_name=None))
        self.assertEqual(result.candidate.venue_name, '')
        self.assertIsNone(result.result.venue_name)

    def test_invented_title_venue_address_and_price_removed(self):
        mapping = suggestion(title='Invented artist', venue_name='Invented venue',
                             address='Invented road 123', price='999.00')
        result = self.run_result(mapping).result
        self.assertIsNone(result.title)
        self.assertIsNone(result.venue_name)
        self.assertIsNone(result.address)
        self.assertIsNone(result.price)
        self.assertIn('Unsupported', ' '.join(result.ambiguities))

    def test_false_evidence_quote_and_wrong_timezone_removed(self):
        mapping = suggestion(start_time='2026-10-12T19:00:00+00:00')
        mapping['evidence']['title'] = [{'source': 'caption', 'quote': 'Invented artist'}]
        result = self.run_result(mapping).result
        self.assertIsNone(result.title)
        self.assertIsNone(result.start_time)

    def test_multiple_events_review_without_candidate_and_cached(self):
        result = self.run_result(suggestion(event_count=2))
        self.assertEqual(result.action, 'review')
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status, 'new')
        self.assertEqual(self.raw.raw_metadata['event_extraction']['result']['event_count'], 2)
        self.assertEqual(self.run_result().action, 'review')
        self.transport.assert_called_once()

    def test_unresolved_event_count_requires_review(self):
        result = self.run_result(suggestion(event_count=None))
        self.assertEqual(result.action, 'review')
        self.assertFalse(EventCandidate.objects.exists())

    def test_explicit_free_price(self):
        self.raw.raw_text = 'Концерт: ДахаБраха\nВхід вільний'
        self.raw.raw_metadata['image_text'] = 'ДахаБраха\n12 ЖОВТНЯ\n19:00'
        mapping = suggestion(price='0.00', venue_name=None)
        mapping['evidence']['price'] = [{'source': 'caption', 'quote': 'Вхід вільний'}]
        result = self.run_result(mapping)
        self.assertEqual(result.result.price, Decimal(0))

    def test_minimum_price_has_note(self):
        result = self.run_result()
        self.assertEqual(result.result.price, Decimal(700))
        self.assertIn('Minimum/base price', result.result.extraction_notes)

    def test_bad_category_is_null(self):
        result = self.run_result(suggestion(category='invalid-category')).result
        self.assertIsNone(result.category)
        self.assertLessEqual(result.confidence, .7)

    def test_schema_error_and_malformed_json_fall_back(self):
        for mapping in ({'is_event': True}, {**suggestion(), 'latitude': 49.8}):
            with self.subTest(mapping=list(mapping)), self.assertLogs('ingestion.extraction', level='WARNING'):
                result = self.run_result(mapping, dry_run=True)
            self.assertEqual(result.action, 'review')
            self.assertIn('deterministic fallback', result.result.extraction_notes)
        broken = response({})
        broken.iter_content.return_value = [b'not JSON TEST_SECRET_KEY']
        self.transport.return_value = broken
        with self.assertLogs('ingestion.extraction', level='WARNING') as logs:
            result = self.run_result(dry_run=True)
        self.assertIn('deterministic fallback', result.result.extraction_notes)
        self.assertNotIn('TEST_SECRET_KEY', str(logs.output))

    def test_timeout_fallback_is_one_request(self):
        self.transport.side_effect = Timeout('TEST_SECRET_KEY')
        with self.assertLogs('ingestion.extraction', level='WARNING') as logs:
            result = self.run_result()
        self.assertEqual(result.action, 'review')
        self.assertFalse(EventCandidate.objects.exists())
        self.assertIsNone(result.result.start_time)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'provider_timeout')
        self.assertNotIn('TEST_SECRET_KEY', str(logs.output))
        self.transport.assert_called_once()

    def test_missing_key_and_model_fall_back_without_request(self):
        for key, model in (('', 'test-model'), ('test-key', '')):
            self.provider = OpenAIEventExtractionProvider(api_key=key, model=model, transport=self.transport)
            with self.assertLogs('ingestion.extraction', level='WARNING'):
                result = self.run_result(dry_run=True)
            self.assertEqual(result.action, 'review')
        self.transport.assert_not_called()

    def test_configuration_default_and_explicit(self):
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'deterministic'}):
            self.assertIsNone(configured_provider())
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'openai', 'OPENAI_API_KEY': '', 'EVENT_EXTRACTION_MODEL': ''}):
            configured = configured_provider()
            self.assertIsInstance(configured, OpenAIEventExtractionProvider)
            with self.assertLogs('ingestion.extraction', level='WARNING'):
                self.assertEqual(extract_raw_source_item(self.raw, provider=configured, dry_run=True).action, 'review')

    def test_idempotence_preserves_admin_edits(self):
        candidate = self.run_result().candidate
        candidate.title = 'Moderator corrected title'
        candidate.save()
        result = self.run_result()
        self.assertEqual(result.action, 'skipped')
        candidate.refresh_from_db()
        self.assertEqual(candidate.title, 'Moderator corrected title')
        self.assertEqual(EventCandidate.objects.count(), 1)
        self.transport.assert_called_once()

    def test_text_only_bounded_request_and_strict_schema(self):
        self.run_result(dry_run=True)
        request = self.transport.call_args.kwargs
        payload = request['json']
        self.assertEqual(payload['model'], 'configured-test-model')
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(request['timeout'], (5, 90))
        self.assertFalse(request['allow_redirects'])
        supplied = json.loads(payload['input'][0]['content'][0]['text'])
        serialized = json.dumps(supplied)
        for secret in ('SECRET_COOKIE', 'SECRET_BROWSER', 'PRIVATE', 'raw_metadata', 'raw_image_url'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(supplied['timezone'], 'Europe/Kyiv')
        self.assertEqual(supplied['deterministic_observations']['sources']['ocr']['dates'], ['2026-10-12'])
        self.assertEqual(payload['input'][0]['content'][0]['type'], 'input_text')

    def test_long_inputs_are_capped_and_flagged(self):
        self.raw.raw_text += '\n' + 'x' * 20000
        self.raw.raw_metadata['image_text'] += '\n' + 'y' * 40000
        result = self.run_result(dry_run=True)
        supplied = json.loads(self.transport.call_args.kwargs['json']['input'][0]['content'][0]['text'])
        self.assertEqual(len(supplied['caption']), 8000)
        self.assertEqual(len(supplied['ocr']), 16000)
        self.assertTrue(supplied['input_truncated'])
        self.assertLessEqual(result.result.confidence, .55)

    def test_dry_run_diagnostics_and_no_writes_or_public_leaks(self):
        output = StringIO()
        before = dict(self.raw.raw_metadata)
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=self.provider):
            call_command('extract_events', source='festrepublic', limit=1, dry_run=True, debug_json=True, stdout=output)
        line = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(line['provider'], 'openai')
        self.assertEqual(line['source'], '@festrepublic')
        self.assertEqual(line['provenance']['start_time'], ['ocr'])
        self.assertTrue(line['result']['needs_review'])
        self.assertIsNone(line['deterministic_result']['start_time'])
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata, before)
        self.assertEqual(self.raw.extraction_status, 'new')
        self.assertFalse(EventCandidate.objects.exists())
        self.assertFalse(Event.objects.exists())
        public = self.client.get('/api/events/')
        self.assertEqual(public.status_code, 200)
        self.assertNotIn('event_extraction', public.content.decode())
        self.assertNotIn('SECRET', output.getvalue() + public.content.decode())

    def test_unknown_count_does_not_restore_unknown_model_fields(self):
        mapping = suggestion(event_count=None, title=None, venue_name=None, price=None)
        result = self.run_result(mapping)
        self.assertEqual(result.action, 'review')
        self.assertIsNone(result.result.title)
        self.assertIsNone(result.result.venue_name)
        self.assertIsNone(result.result.price)

    def test_unstated_end_time_removed(self):
        mapping = suggestion(end_time='2026-10-12T22:00:00+03:00')
        mapping['evidence']['end_time'] = [{'source': 'ocr', 'quote': '12 ЖОВТНЯ\n19:00'}]
        self.assertIsNone(self.run_result(mapping).result.end_time)

    def test_model_change_invalidates_review_cache(self):
        self.run_result(suggestion(event_count=2))
        self.raw.refresh_from_db()
        self.provider.model = 'another-configured-model'
        self.assertEqual(self.run_result().action, 'review')
        self.assertEqual(self.transport.call_count, 2)

    def test_http_failure_and_refusal_fall_back_without_retry(self):
        rejected = response(suggestion())
        rejected.status_code = 429
        self.transport.return_value = rejected
        with self.assertLogs('ingestion.extraction', level='WARNING'):
            self.assertEqual(self.run_result(dry_run=True).action, 'review')
        self.transport.assert_called_once()
        refused = response({})
        refused.iter_content.return_value = [json.dumps({'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'SECRET_RESPONSE'}]}]}).encode()]
        self.transport.return_value = refused
        with self.assertLogs('ingestion.extraction', level='WARNING') as logs:
            self.assertEqual(self.run_result(dry_run=True).action, 'review')
        self.assertNotIn('SECRET_RESPONSE', str(logs.output))

    def test_non_event_remains_ignored(self):
        mapping = suggestion(is_event=False, event_count=0, title=None, start_time=None,
                             venue_name=None, price=None, category=None)
        self.assertEqual(self.run_result(mapping).action, 'ignored')
        self.assertFalse(EventCandidate.objects.exists())
