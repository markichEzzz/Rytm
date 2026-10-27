"""Bounded text-only local Ollama HTTP adapter; no CLI, images, or retries."""
from dataclasses import replace
from hashlib import sha256
import json
import os
import time
from urllib.parse import urlsplit

import requests

from .contracts import ExtractionBatch, ExtractionError, MULTI_EXTRACTION_JSON_SCHEMA
from .openai import ProviderUnavailable
from .structured import PROMPT, build_input


class OllamaEventExtractionProvider:
    name = 'ollama'

    def __init__(self, *, base_url=None, model=None, transport=None, read_timeout=None):
        self.base_url = (base_url if base_url is not None else
                         os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')).strip().rstrip('/')
        self.model = (model if model is not None else os.environ.get('OLLAMA_MODEL', 'llama3.1:8b')).strip()
        self.transport = transport or requests.post
        try:
            self.read_timeout = float(read_timeout if read_timeout is not None else
                                      os.environ.get('OLLAMA_READ_TIMEOUT', '180'))
        except (TypeError, ValueError):
            self.read_timeout = 180
        if not 1 <= self.read_timeout <= 600:
            self.read_timeout = 180
        self.response_seconds = None

    def extract(self, raw_item, context):
        self.response_seconds = None
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ProviderUnavailable('ollama_invalid_base_url')
        if not self.model:
            raise ProviderUnavailable('missing_model')
        supplied = build_input(raw_item, context)
        fingerprint = sha256(json.dumps({'provider': self.name, 'model': self.model,
            'endpoint': self.base_url, 'version': 2,
            'input': {**supplied, 'current_time': supplied['current_time'][:10]}},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cache = (raw_item.raw_metadata or {}).get('event_extraction', {})
        if cache.get('fingerprint') == fingerprint and cache.get('provider') == self.name:
            try:
                return replace(ExtractionBatch.from_mapping(cache['batch']),
                               unresolved=tuple(cache.get('unresolved_schema', [])))
            except (ExtractionError, KeyError, TypeError, ValueError):
                pass
        payload = {'model': self.model, 'stream': False, 'format': MULTI_EXTRACTION_JSON_SCHEMA,
            'messages': [{'role': 'system', 'content': PROMPT},
                         {'role': 'user', 'content': json.dumps(supplied, ensure_ascii=False)}],
            'options': {'temperature': 0, 'num_ctx': 16384, 'num_predict': 8192}}
        started = time.monotonic()
        try:
            with self.transport(self.base_url + '/api/chat', json=payload,
                    timeout=(5, self.read_timeout), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    # This dedicated endpoint requests a model: 404 is a missing model.
                    code = 'ollama_model_not_found' if response.status_code == 404 else 'ollama_http_failure'
                    raise ProviderUnavailable(code, http_status=response.status_code)
                data = bytearray()
                for chunk in response.iter_content(16 * 1024):
                    if len(data) + len(chunk) > 256 * 1024 or time.monotonic() - started > self.read_timeout + 5:
                        raise ProviderUnavailable('ollama_response_limit')
                    data.extend(chunk)
            try:
                body = json.loads(data)
                if not isinstance(body, dict) or body.get('done') is not True or body.get('done_reason') == 'length':
                    raise ProviderUnavailable('ollama_incomplete_output')
                message = body.get('message')
                if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                    raise ProviderUnavailable('ollama_invalid_json')
                mapping = json.loads(message['content'])
            except (ValueError, TypeError):
                raise ProviderUnavailable('ollama_invalid_json') from None
            try:
                result = ExtractionBatch.from_mapping(mapping)
            except (ExtractionError, TypeError, ValueError, AttributeError):
                raise ProviderUnavailable('ollama_validation_failure') from None
            self.response_seconds = round(time.monotonic() - started, 3)
            raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'event_extraction': {
                'provider': self.name, 'model': self.model, 'fingerprint': fingerprint,
                'batch': mapping, 'provider_batch': mapping, 'status': 'complete',
                'provider_response_seconds': self.response_seconds,
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
            raise ProviderUnavailable('ollama_timeout') from None
        except requests.ConnectionError:
            raise ProviderUnavailable('ollama_unavailable') from None
        except Exception:
            raise ProviderUnavailable('ollama_invalid_or_unavailable') from None
        finally:
            self.response_seconds = round(time.monotonic() - started, 3)
