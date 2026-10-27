"""Ollama adapter and shared pipeline regressions; HTTP is always mocked."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from io import StringIO
import json
import os
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase
import requests

from .extraction import configured_provider
from .extractors.contracts import CATEGORIES, MULTI_EXTRACTION_JSON_SCHEMA
from .extractors.ollama import OllamaEventExtractionProvider
from .extractors.openai import OpenAIEventExtractionProvider
from .models import EventCandidate
from . import test_multi_extraction as fixtures


def response(mapping=None, *, content=None, status=200, **updates):
    body = {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant',
            'content': content if content is not None else json.dumps(mapping, ensure_ascii=False)}, **updates}
    result = Mock(status_code=status)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [json.dumps(body).encode()]
    return result


class OllamaTests(TestCase):
    extract = fixtures.MultiExtractionTests.extract

    def setUp(self):
        fixtures.MultiExtractionTests.setUp(self)
        self.provider = OllamaEventExtractionProvider(transport=self.transport,
            base_url='http://127.0.0.1:11434', model='llama3.1:8b')
        self.set_events(1)

    def set_events(self, count=1):
        fixtures.MultiExtractionTests.set_events(self, count)
        self.transport.return_value = response(self.mapping)

    def output(self):
        self.transport.return_value = response(self.mapping)

    def command(self, **options):
        output = StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=self.provider):
            call_command('extract_events', item_id=self.raw.pk, stdout=output, **options)
        return output.getvalue()

    def test_selection_without_openai_key_and_safe_defaults(self):
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'ollama'}, clear=True):
            provider = configured_provider()
        self.assertIsInstance(provider, OllamaEventExtractionProvider)
        self.assertEqual(provider.base_url, 'http://127.0.0.1:11434')
        self.assertEqual(provider.model, 'llama3.1:8b')
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'ollama',
                'OLLAMA_BASE_URL': 'http://localhost:11435/', 'OLLAMA_MODEL': 'local-model',
                'OLLAMA_READ_TIMEOUT': '240'}, clear=True):
            provider = configured_provider()
        self.assertEqual((provider.base_url, provider.model, provider.read_timeout),
                         ('http://localhost:11435', 'local-model', 240))

    def test_one_event_shared_validation_and_http_contract(self):
        outcome = self.extract(dry_run=True)
        self.assertEqual(outcome.ready, 1)
        self.assertEqual(outcome.results[0].title, 'Гурт 0')
        self.assertEqual(outcome.results[0].start_time.utcoffset().total_seconds(), 10800)
        self.assertEqual(outcome.results[0].category, 'concert')
        args, kwargs = self.transport.call_args
        self.assertEqual(args, ('http://127.0.0.1:11434/api/chat',))
        self.assertEqual(kwargs['json']['format'], MULTI_EXTRACTION_JSON_SCHEMA)
        self.assertFalse(kwargs['json']['stream'])
        self.assertEqual(kwargs['timeout'], (5, 180))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('headers', kwargs)
        self.assertIsNotNone(self.raw.raw_metadata['event_extraction']['provider_response_seconds'])

    def test_multiple_events_create_separate_candidates(self):
        self.set_events(3)
        outcome = self.extract()
        self.assertEqual((outcome.detected, outcome.ready, len(outcome.candidates)), (3, 3, 3))
        self.assertEqual(EventCandidate.objects.count(), 3)

    def test_invalid_json_falls_back_without_crashing_batch(self):
        self.transport.return_value = response(content='```json\nnot valid\n```')
        self.extract(dry_run=True)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_invalid_json')

    def test_invalid_envelope_is_validation_failure(self):
        self.transport.return_value = response({'events': 'bad'})
        self.extract(dry_run=True)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_validation_failure')

    def test_timeout_fallback(self):
        self.transport.side_effect = requests.Timeout('PRIVATE_EXCEPTION')
        output = self.command(dry_run=True)
        self.assertIn('ollama_timeout', output)
        self.assertIn('Provider: ollama | Model: llama3.1:8b', output)
        self.assertNotIn('PRIVATE_EXCEPTION', output)
        self.assertFalse(EventCandidate.objects.exists())

    def test_connection_failure_fallback(self):
        self.transport.side_effect = requests.ConnectionError('PRIVATE_CONNECTION')
        self.extract(dry_run=True)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_unavailable')

    def test_missing_model_response(self):
        self.transport.return_value = response(status=404)
        self.extract(dry_run=True)
        audit = self.raw.raw_metadata['event_extraction']
        self.assertEqual(audit['fallback_reason'], 'ollama_model_not_found')
        self.assertEqual(audit['provider_http_status'], 404)

    def test_missing_price_stays_unknown_null(self):
        self.mapping['events'][0]['price'] = None
        self.output()
        candidate = self.extract().candidate
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_minimum_price_remains_paid_and_noted(self):
        self.raw.raw_text += '\nКвитки від 300 грн'
        self.raw.save()
        entry = self.mapping['events'][0]
        entry['price'] = '300'
        entry['evidence']['price'] = [{'source': 'caption', 'quote': 'від 300 грн'}]
        self.output()
        candidate = self.extract().candidate
        self.assertEqual((candidate.price_status, candidate.price), ('paid', Decimal('300')))
        self.assertIn('Minimum', candidate.extraction_notes)

    def test_explicit_free_stays_free_zero(self):
        self.raw.raw_text += '\nВхід вільний'
        self.raw.save()
        self.mapping['events'][0]['price'] = '0'
        self.mapping['events'][0]['evidence']['price'] = [{'source': 'caption', 'quote': 'Вхід вільний'}]
        self.output()
        candidate = self.extract().candidate
        self.assertEqual((candidate.price_status, candidate.price), ('free', Decimal('0')))

    def test_hallucinated_price_never_becomes_free_or_paid(self):
        self.mapping['events'][0]['price'] = '0'
        self.output()
        candidate = self.extract().candidate
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_invalid_category_is_safe(self):
        self.mapping['events'][0]['category'] = 'invented_category'
        self.output()
        outcome = self.extract(dry_run=True)
        self.assertNotEqual(outcome.results[0].category, 'invented_category')
        self.assertIn(outcome.results[0].category, {*CATEGORIES, None})

    def test_past_event_rejected(self):
        self.raw.raw_text = self.raw.raw_text.replace('8 жовтня', '6 жовтня')
        self.raw.save()
        entry = self.mapping['events'][0]
        entry['start_time'] = '2026-10-06T19:00:00+03:00'
        entry['evidence']['start_time'][0]['quote'] = '6 жовтня 19:00'
        self.output()
        outcome = self.extract()
        self.assertEqual(outcome.ready, 0)
        self.assertFalse(EventCandidate.objects.exists())
        self.assertIn('past', str(outcome.unresolved))

    def draft(self, **values):
        self.raw.extraction_status = 'extracted'
        self.old_audit = {'provider': 'openai', 'status': 'fallback'}
        self.raw.raw_metadata['event_extraction'] = deepcopy(self.old_audit)
        self.raw.save()
        return EventCandidate.objects.create(raw_source_item=self.raw, **values)

    def test_reextract_retries_ollama_and_preserves_id(self):
        candidate = self.draft()
        outcome = self.extract(reextract=True)
        self.transport.assert_called_once()
        self.assertEqual(outcome.repaired, 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title, 'Гурт 0')
        self.assertEqual(EventCandidate.objects.count(), 1)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['event_extraction']['retry_provider'], 'ollama')
        self.assertEqual(self.raw.raw_metadata['event_extraction_history'], [self.old_audit])

    def test_repair_dry_run_and_moderator_fields(self):
        candidate = self.draft(title='Moderator title', price_status='unknown',
            extraction_notes='[Candidate facts] Moderator edited: title\n[Pricing] Moderator edited: price_status')
        before = list(EventCandidate.objects.values())
        raw_before = deepcopy(self.raw.raw_metadata)
        output = self.command(reextract=True, dry_run=True)
        self.assertIn('Candidates repaired (preview): 1', output)
        self.assertIn('Model response time:', output)
        self.assertNotIn('title: Moderator title ->', output)
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata, raw_before)
        outcome = self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(outcome.repaired, 1)
        self.assertEqual((candidate.title, candidate.price_status, candidate.price), ('Moderator title', 'unknown', None))

    def test_retry_failure_preserves_candidate(self):
        self.draft(title='Keep title')
        before = list(EventCandidate.objects.values())
        self.transport.side_effect = requests.Timeout()
        outcome = self.extract(reextract=True)
        self.assertEqual(outcome.action, 'repair_failed')
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_timeout')

    def test_bounded_text_excludes_private_metadata_and_images(self):
        self.raw.raw_metadata.update(cookies='SECRET_COOKIE', session='SECRET_SESSION')
        self.raw.raw_text += 'x' * 9000
        self.raw.raw_metadata['image_text'] = 'y' * 17000
        self.extract(dry_run=True)
        payload = self.transport.call_args.kwargs['json']
        text = json.dumps(payload)
        self.assertNotIn('SECRET_', text)
        self.assertNotIn('PRIVATE_IMAGE', text)
        supplied = json.loads(payload['messages'][1]['content'])
        self.assertEqual(len(supplied['caption']), 8000)
        self.assertEqual(len(supplied['ocr']), 16000)
        self.assertEqual(supplied['allowed_categories'], sorted(CATEGORIES))
        self.assertEqual(supplied['published_at'], self.raw.published_at.isoformat())

    def test_cache_used_normally_but_bypassed_on_repair(self):
        self.extract(dry_run=True)
        self.extract(dry_run=True)
        self.transport.assert_called_once()
        self.raw.save()
        EventCandidate.objects.create(raw_source_item=self.raw)
        self.extract(reextract=True, dry_run=True)
        self.assertEqual(self.transport.call_count, 2)

    def test_truncated_and_oversized_output_fail_safely(self):
        self.transport.return_value = response(self.mapping, done_reason='length')
        self.extract(dry_run=True)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_incomplete_output')
        self.transport.return_value = response(self.mapping)
        self.transport.return_value.iter_content.return_value = [b'x' * (256 * 1024 + 1)]
        self.extract(dry_run=True)
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_response_limit')

    def test_existing_providers_still_selected_and_deterministic_works(self):
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'openai'}, clear=True):
            self.assertIsInstance(configured_provider(), OpenAIEventExtractionProvider)
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'deterministic'}, clear=True):
            self.assertIsNone(configured_provider())
        from .extraction import extract_raw_source_item
        extract_raw_source_item(self.raw, provider=None, dry_run=True)
        self.transport.assert_not_called()

    def test_invalid_configuration_does_not_send_request(self):
        self.provider.base_url = 'http://user:secret@localhost:11434/?secret=bad'
        self.extract(dry_run=True)
        self.transport.assert_not_called()
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'ollama_invalid_base_url')
