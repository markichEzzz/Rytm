"""Private, bounded poster OCR. Images exist only in memory; no media storage calls."""
import csv
from collections import OrderedDict
from functools import lru_cache
from hashlib import sha256
from io import BytesIO, StringIO
import os
import shutil
import subprocess
import time
from typing import Protocol
from urllib.parse import urlparse

from PIL import Image
import requests

MAX_IMAGES = 3
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_TEXT = 24000
MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


class ImageTextExtractor(Protocol):
    def extract_text(self, image_bytes: bytes) -> str: ...


class ImageTextError(Exception):
    """Safe error code, never a signed URL, HTTP body, or subprocess output."""


def download_image(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        # Source URLs are untrusted editable metadata; restrict to Instagram CDN origins.
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host.endswith('.' + domain) for domain in ('cdninstagram.com', 'fbcdn.net', 'telesco.pe'))):
            raise ImageTextError('unsupported_image_origin')
        with requests.get(url, stream=True, timeout=(5, 10), allow_redirects=False) as response:
            if response.status_code != 200:
                raise ImageTextError('image_http_failure')
            if response.headers.get('Content-Type', '').split(';')[0].strip().lower() not in MIME_TYPES:
                raise ImageTextError('unsupported_image_mime')
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_BYTES:
                raise ImageTextError('image_too_large')
            data = bytearray()
            started = time.monotonic()
            for chunk in response.iter_content(64 * 1024):
                if time.monotonic() - started > 20:
                    raise ImageTextError('image_download_timeout')
                if len(data) + len(chunk) > MAX_BYTES:
                    raise ImageTextError('image_too_large')
                data.extend(chunk)
            if not data:
                raise ImageTextError('empty_image')
            return bytes(data)
    except ImageTextError:
        raise
    except Exception:
        raise ImageTextError('image_download_failure') from None


class TesseractImageTextExtractor:
    def __init__(self, executable, languages='ukr+eng'):
        self.executable, self.languages = executable, languages

    def extract_text(self, image_bytes):
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > MAX_PIXELS:
                    raise ImageTextError('unsupported_image_dimensions_or_format')
                output = BytesIO()
                image.convert('RGB').save(output, format='PNG')
            # stdin/stdout avoids retaining original or converted images on disk.
            result = subprocess.run([self.executable, 'stdin', 'stdout', '-l', self.languages],
                                    input=output.getvalue(), capture_output=True, timeout=20, check=True)
            return result.stdout.decode('utf-8', errors='replace').strip()[:MAX_TEXT]
        except ImageTextError:
            raise
        except Exception:
            raise ImageTextError('local_ocr_failure') from None

    def extract_layout(self, image_bytes):
        """One Tesseract TSV request; retain word geometry as ordered line blocks."""
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > MAX_PIXELS:
                    raise ImageTextError('unsupported_image_dimensions_or_format')
                width, height = image.size
                output = BytesIO()
                image.convert('RGB').save(output, format='PNG')
            result = subprocess.run([self.executable, 'stdin', 'stdout', '-l', self.languages, '-c', 'tessedit_create_tsv=1'],
                                    input=output.getvalue(), capture_output=True, timeout=20, check=True)
            lines = OrderedDict()
            rows = csv.DictReader(StringIO(result.stdout[:2_000_000].decode('utf-8', errors='replace')), delimiter='\t')
            required = {'level', 'page_num', 'block_num', 'par_num', 'line_num', 'left', 'top', 'width', 'height', 'conf', 'text'}
            if not rows.fieldnames or not required <= set(rows.fieldnames):
                raise ImageTextError('local_ocr_invalid_layout')
            for index, row in enumerate(rows):
                if index >= 5000:
                    break
                if row.get('level') != '5' or not (row.get('text') or '').strip():
                    continue
                try:
                    key = tuple(int(row[name]) for name in ('page_num', 'block_num', 'par_num', 'line_num'))
                    x, y, w, h = (int(row[name]) for name in ('left', 'top', 'width', 'height'))
                    confidence = float(row['conf'])
                    if min(x, y, w, h) < 0 or not 0 <= confidence <= 100:
                        continue
                    lines.setdefault(key, []).append((row['text'].strip(), x, y, w, h, confidence))
                except (ValueError, KeyError, TypeError):
                    continue
            blocks = []
            for key, words in list(lines.items())[:240]:
                x, y = min(w[1] for w in words), min(w[2] for w in words)
                blocks.append({'text': ' '.join(w[0] for w in words)[:500], 'x': x, 'y': y,
                    'width': max(w[1] + w[3] for w in words) - x, 'height': max(w[2] + w[4] for w in words) - y,
                    'confidence': round(sum(w[5] for w in words) / len(words), 1), 'confidence_min': min(w[5] for w in words),
                    'order': len(blocks), 'page': key[0], 'block': key[1], 'paragraph': key[2], 'line': key[3]})
            return {'text': '\n'.join(block['text'] for block in blocks)[:MAX_TEXT],
                    'width': width, 'height': height, 'blocks': blocks,
                    'truncated': len(result.stdout) > 2_000_000 or len(lines) > 240 or ('index' in locals() and index >= 4999)}
        except ImageTextError:
            raise
        except Exception:
            raise ImageTextError('local_ocr_failure') from None


@lru_cache(maxsize=8)
def _local_provider(executable, languages, tessdata):
    if not executable:
        return None, 'ocr_engine_unavailable'
    try:
        result = subprocess.run([executable, '--list-langs'], capture_output=True, timeout=5, check=True)
        available = set(result.stdout.decode().splitlines()[1:])
        if not set(languages.split('+')) <= available:
            return None, 'ocr_language_data_unavailable'
    except Exception:
        return None, 'ocr_engine_unavailable'
    return TesseractImageTextExtractor(executable, languages), ''


def configured_image_extractor():
    name = os.environ.get('EVENT_IMAGE_TEXT_PROVIDER', 'tesseract').lower()
    if name == 'disabled':
        return None, 'ocr_disabled'
    if name != 'tesseract':
        return None, 'ocr_provider_unavailable'
    return _local_provider(shutil.which(os.environ.get('EVENT_OCR_EXECUTABLE', 'tesseract')),
                           os.environ.get('EVENT_OCR_LANGUAGES', 'ukr+eng'), os.environ.get('TESSDATA_PREFIX', ''))


def image_urls(raw_item):
    metadata = raw_item.raw_metadata or {}
    extras = metadata.get('image_urls', [])
    if not isinstance(extras, (list, tuple)):
        extras = []
    return list(dict.fromkeys(url for url in [raw_item.raw_image_url, *extras]
                              if isinstance(url, str) and url))[:MAX_IMAGES]


def prepare_image_text(raw_item, *, extractor=None, downloader=None):
    """Update only the in-memory metadata; orchestration decides whether to save.

    Completed/failed attempts are cached against URLs and engine configuration.
    Missing dependencies are not cached, so installing OCR allows the next extraction.
    """
    metadata = dict(raw_item.raw_metadata or {})
    raw_item.raw_metadata = metadata
    urls = image_urls(raw_item)
    if not urls or (metadata.get('image_text') and 'image_text_cache' not in metadata):
        return
    engine, unavailable = (extractor, '') if extractor is not None else configured_image_extractor()
    if engine is None:
        metadata['image_text_status'] = unavailable
        return
    fingerprint = sha256(('\n'.join(urls) + '\n' + type(engine).__name__ + '\n'
                          + getattr(engine, 'languages', '') + '\n' + getattr(engine, 'executable', '') + '\nlayout-v1').encode()).hexdigest()
    if metadata.get('image_text_cache') == fingerprint:
        return
    texts, errors, layouts = [], [], []
    for index, url in enumerate(urls, 1):
        try:
            data = (downloader or download_image)(url)
            if callable(getattr(engine, 'extract_layout', None)):
                layout = engine.extract_layout(data)
                text = layout['text'].strip()[:MAX_TEXT // MAX_IMAGES]
                layouts.append({**layout, 'image_index': index})
                layouts[-1].pop('text', None)
            else:
                text = engine.extract_text(data).strip()[:MAX_TEXT // MAX_IMAGES]
            if text:
                texts.append(f'IMAGE {index}:\n{text}')
        except Exception as error:
            errors.append(str(error) if isinstance(error, ImageTextError) else 'image_processing_failure')
    metadata.update(image_text='\n\n'.join(texts), image_text_layout=layouts, image_text_source='ocr',
                    image_text_provider=type(engine).__name__, image_text_cache=fingerprint,
                    image_text_status='partial' if errors and texts else ('failed' if errors else 'complete'),
                    image_text_errors=errors, image_text_images_attempted=len(urls))
