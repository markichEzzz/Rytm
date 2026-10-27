"""One bounded text-only Responses API request; no SDK, images, or automatic retries."""
from hashlib import sha256
import json
import os
import time

import requests

from .contracts import (CATEGORIES, EVIDENCE_FIELDS, EXTRACTION_JSON_SCHEMA, ExtractionError,
                        ExtractionResult, ExtractionBatch, MULTI_EXTRACTION_JSON_SCHEMA, KYIV)
from .hybrid import CAPTION_LIMIT, OCR_LIMIT, result_mapping

from .structured import PROMPT, build_input


class ProviderUnavailable(ExtractionError):
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


class UnavailableProvider:
    def __init__(self, reason, name='unavailable'):
        self.reason, self.name = reason, name

    def extract(self, raw_item, context):
        raise ProviderUnavailable(self.reason)


class OpenAIEventExtractionProvider:
    name = 'openai'

    def __init__(self, *, api_key=None, model=None, transport=None):
        self.api_key = api_key if api_key is not None else os.environ.get('OPENAI_API_KEY', '').strip()
        # Project preference; explicit configuration always wins, with no substitution on failure.
        self.model = model if model is not None else os.environ.get('EVENT_EXTRACTION_MODEL', 'gpt-6-luna').strip()
        self.transport = transport or requests.post

    def build_input(self, raw_item, context):
        return build_input(raw_item, context)

    def extract(self, raw_item, context):
        if not self.api_key:
            raise ProviderUnavailable('missing_api_key')
        if not self.model:
            raise ProviderUnavailable('missing_model')
        supplied = self.build_input(raw_item, context)
        fingerprint_input = {**supplied, 'current_time': supplied['current_time'][:10]}
        fingerprint = sha256(json.dumps({'model': self.model, 'input': fingerprint_input, 'version': 4},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cache = (raw_item.raw_metadata or {}).get('event_extraction', {})
        if cache.get('fingerprint') == fingerprint and cache.get('provider') == self.name:
            try:
                if 'batch' in cache:
                    from dataclasses import replace
                    return replace(ExtractionBatch.from_mapping(cache['batch']), unresolved=tuple(cache.get('unresolved_schema', [])))
                return ExtractionResult.from_mapping(cache['result'])
            except (ExtractionError, KeyError, TypeError, ValueError):
                pass
        payload = {
            'model': self.model, 'store': False, 'max_output_tokens': 18000,
            'instructions': PROMPT,
            'input': [{'role': 'user', 'content': [{'type': 'input_text',
                        'text': json.dumps(supplied, ensure_ascii=False)}]}],
            'text': {'format': {'type': 'json_schema', 'name': 'event_extraction',
                               'strict': True, 'schema': MULTI_EXTRACTION_JSON_SCHEMA}},
        }
        try:
            with self.transport('https://api.openai.com/v1/responses', json=payload,
                    headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'},
                    timeout=(5, 90), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ProviderUnavailable('provider_http_failure', http_status=response.status_code)
                data = bytearray()
                started = time.monotonic()
                for chunk in response.iter_content(16 * 1024):
                    if len(data) + len(chunk) > 256 * 1024 or time.monotonic() - started > 120:
                        raise ProviderUnavailable('provider_response_limit')
                    data.extend(chunk)
            body = json.loads(data)
            if body.get('status') != 'completed':
                raise ProviderUnavailable('provider_incomplete_or_refused')
            messages = [block for item in body.get('output', []) if item.get('type') == 'message'
                        for block in item.get('content', [])]
            if len(messages) != 1 or messages[0].get('type') != 'output_text':
                raise ProviderUnavailable('provider_invalid_output')
            mapping = json.loads(messages[0]['text'])
            result = ExtractionBatch.from_mapping(mapping) if 'events' in mapping else ExtractionResult.from_mapping(mapping)
            if isinstance(result, ExtractionResult) and (set(result.evidence) != set(EVIDENCE_FIELDS) or result.is_event != (result.event_count != 0)):
                raise ProviderUnavailable('provider_invalid_schema')
            if supplied['input_truncated'] and isinstance(result, ExtractionResult):
                from dataclasses import replace
                result = replace(result, confidence=min(result.confidence, .55), needs_review=True,
                    ambiguities=(*result.ambiguities[:11], 'Source input truncated; manual review required.'))
            raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'event_extraction': {
                'provider': self.name, 'model': self.model, 'fingerprint': fingerprint,
                **({'batch': mapping, 'provider_batch': mapping} if isinstance(result, ExtractionBatch) else {'result': result_mapping(result)}),
                'status': 'complete',
                'deterministic_result': context.deterministic_observations['result'],
                'observations': context.deterministic_observations['sources'],
                'location_hints': context.deterministic_observations.get('location_hints', []),
                'source_account_hint': {'value': context.source_display_name or context.source_username,
                                        'scope': 'source_account_hint', 'used_as_location_fact': False},
            }}
            return result
        except ProviderUnavailable:
            raise
        except requests.Timeout:
            raise ProviderUnavailable('provider_timeout') from None
        except Exception:
            raise ProviderUnavailable('provider_invalid_or_unavailable') from None
