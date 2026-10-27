"""Explicit provider selection stays predictable; failures never call Ollama."""
import os
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
import requests

from .extraction import configured_provider
from .extractors.openai import OpenAIEventExtractionProvider
from .test_candidate_repair import CandidateRepairTests
from . import test_multi_extraction as fixtures
from .test_hybrid import response


class PrimaryProviderSelectionTests(SimpleTestCase):
    def test_openai_and_preferred_model_are_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = configured_provider()
        self.assertIsInstance(provider, OpenAIEventExtractionProvider)
        self.assertEqual(provider.model, 'gpt-6-luna')

    def test_explicit_openai_ignores_ollama_configuration(self):
        with patch.dict(os.environ, {'EVENT_EXTRACTION_PROVIDER': 'openai',
                'EVENT_EXTRACTION_MODEL': 'gpt-6-luna', 'OLLAMA_BASE_URL': 'invalid-url'}, clear=True), \
                patch('ingestion.extractors.ollama.OllamaEventExtractionProvider',
                      side_effect=AssertionError('Ollama must not be initialized')) as ollama:
            provider = configured_provider()
        self.assertEqual((provider.name, provider.model), ('openai', 'gpt-6-luna'))
        ollama.assert_not_called()


class PrimaryProviderTests(TestCase):
    setUp = fixtures.MultiExtractionTests.setUp
    set_events = fixtures.MultiExtractionTests.set_events
    extract = fixtures.MultiExtractionTests.extract
    draft = CandidateRepairTests.draft

    def select_openai(self, **values):
        with patch.dict(os.environ, values, clear=True):
            self.provider = configured_provider()
        self.provider.api_key = 'FAKE_TEST_KEY'
        self.provider.transport = self.transport

    def test_configured_openai_retries_and_preserves_moderator_values_without_ollama(self):
        candidate = self.draft(title='Moderator title',
            extraction_notes='[Candidate facts] Moderator edited: title\n[Pricing] Moderator edited: price_status')
        self.select_openai(EVENT_EXTRACTION_PROVIDER='openai', EVENT_EXTRACTION_MODEL='gpt-6-luna',
            OLLAMA_BASE_URL='invalid-url', OLLAMA_MODEL='not-installed')
        with patch('ingestion.extractors.ollama.OllamaEventExtractionProvider.extract',
                   side_effect=AssertionError('OpenAI must never switch to Ollama')) as ollama:
            result = self.extract(reextract=True)
        self.assertEqual(result.repaired, 1)
        self.transport.assert_called_once()
        ollama.assert_not_called()
        self.assertEqual(self.transport.call_args.args[0], 'https://api.openai.com/v1/responses')
        self.assertEqual(self.transport.call_args.kwargs['json']['model'], 'gpt-6-luna')
        candidate.refresh_from_db()
        self.assertEqual((candidate.title, candidate.price, candidate.price_status), ('Moderator title', None, 'unknown'))

    def test_openai_failure_codes_and_http_status_preserve_draft_without_ollama(self):
        candidate = self.draft(title='Keep title')
        self.select_openai(EVENT_EXTRACTION_PROVIDER='openai')
        with patch('ingestion.extractors.ollama.OllamaEventExtractionProvider.extract',
                   side_effect=AssertionError('No implicit Ollama fallback')) as ollama:
            for status in (429, 401):
                with self.subTest(status=status):
                    rejected = response(self.mapping)
                    rejected.status_code = status
                    self.transport.return_value = rejected
                    self.raw.refresh_from_db()
                    outcome = self.extract(reextract=True, dry_run=True)
                    audit = self.raw.raw_metadata['event_extraction']
                    self.assertEqual(outcome.action, 'repair_failed')
                    self.assertEqual(audit['fallback_reason'], 'provider_http_failure')
                    self.assertEqual(audit['provider_http_status'], status)
            self.transport.side_effect = requests.Timeout()
            self.raw.refresh_from_db()
            self.extract(reextract=True, dry_run=True)
            self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'provider_timeout')
            self.transport.side_effect = None
            invalid = response(self.mapping)
            invalid.iter_content.return_value = [b'not-json']
            self.transport.return_value = invalid
            self.raw.refresh_from_db()
            self.extract(reextract=True, dry_run=True)
            self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'], 'provider_invalid_or_unavailable')
        ollama.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.title, 'Keep title')
        self.assertIsNone(candidate.start_time)
