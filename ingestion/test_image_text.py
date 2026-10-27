from datetime import datetime
from decimal import Decimal
from io import BytesIO, StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.exceptions import SynchronousOnlyOperation
from django.test import TestCase, SimpleTestCase
from playwright.sync_api import sync_playwright
from PIL import Image

from events.models import Event
from .extraction import extract_raw_source_item, extract_facts
from .extractors.contracts import KYIV
from .image_text import (prepare_image_text, download_image, ImageTextError, MAX_BYTES,
                         TesseractImageTextExtractor)
from .admin import RawSourceItemAdmin, EventCandidateAdmin
from django.contrib import admin
from .models import SourceAccount, RawSourceItem, EventCandidate
from .test_instagram_playwright import FakeBrowser

NOW = datetime(2026, 10, 7, 12, tzinfo=KYIV)
POSTER = 'LATEXFAUNA\n12 ЖОВТНЯ\n19:00\nFESTREPUBLIC\nКВИТКИ ВІД 700 ГРН'
URL = 'https://scontent.test.cdninstagram.com/private.jpg'


class DownloadTests(SimpleTestCase):
    def response(self, mime='image/png', length=None, chunks=(b'bytes',)):
        response = Mock(status_code=200)
        response.headers = {'Content-Type': mime}
        if length:
            response.headers['Content-Length'] = str(length)
        response.iter_content.return_value = chunks
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_bounded_private_download(self):
        with patch('ingestion.image_text.requests.get', return_value=self.response()) as get:
            self.assertEqual(download_image(URL), b'bytes')
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(get.call_args.kwargs['stream'])
            self.assertEqual(get.call_args.kwargs['timeout'], (5, 10))

    def test_mime_size_and_redirect_rejected(self):
        responses = [self.response('text/html'), self.response(length=MAX_BYTES + 1),
                     self.response(chunks=[b'x' * (MAX_BYTES + 1)])]
        redirect = self.response()
        redirect.status_code = 302
        responses.append(redirect)
        for response in responses:
            with patch('ingestion.image_text.requests.get', return_value=response), self.assertRaises(ImageTextError):
                download_image(URL)
        with patch('ingestion.image_text.requests.get') as get, self.assertRaises(ImageTextError):
            download_image('https://127.0.0.1/private')
        get.assert_not_called()

    def test_local_ocr_uses_memory_and_bounded_subprocess(self):
        data = BytesIO()
        Image.new('RGB', (10, 10)).save(data, 'PNG')
        with patch('ingestion.image_text.subprocess.run', return_value=Mock(stdout=b'Poster')) as run:
            self.assertEqual(TesseractImageTextExtractor('/test/tesseract').extract_text(data.getvalue()), 'Poster')
        self.assertEqual(run.call_args.args[0][1:3], ['stdin', 'stdout'])
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        self.assertTrue(run.call_args.kwargs['input'].startswith(b'\x89PNG'))


class ImageExtractionTests(TestCase):
    def setUp(self):
        self.source = SourceAccount.objects.create(username='festrepublic')
        self.raw = RawSourceItem.objects.create(source_account=self.source, external_id='Poster',
            source_url='https://www.instagram.com/p/Poster/', raw_image_url=URL, published_at=NOW,
            raw_metadata={'original': 'audit'})
        now = patch('django.utils.timezone.now', return_value=NOW)
        now.start()
        self.addCleanup(now.stop)
        self.engine = Mock(spec=['extract_text'])
        self.engine.extract_text.return_value = POSTER
        provider = patch('ingestion.image_text.configured_image_extractor', return_value=(self.engine, ''))
        provider.start()
        self.addCleanup(provider.stop)
        fetch_patch = patch('ingestion.image_text.download_image', return_value=b'MOCK_PRIVATE_BYTES')
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def test_private_image_only_candidate_and_cache(self):
        outcome = extract_raw_source_item(self.raw)
        self.assertEqual(outcome.action, 'created')
        candidate = outcome.candidate
        self.assertEqual(candidate.title, 'LATEXFAUNA')
        self.assertEqual(candidate.start_time.day, 12)
        self.assertEqual(candidate.start_time.hour, 19)
        self.assertEqual(candidate.venue_name, 'FESTREPUBLIC')
        self.assertEqual(candidate.price, Decimal('700'))
        self.assertFalse(candidate.public_photo)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_text, '')
        self.assertEqual(self.raw.raw_metadata['original'], 'audit')
        self.assertIn(POSTER, self.raw.raw_metadata['image_text'])
        prepare_image_text(self.raw)
        self.fetch.assert_called_once()
        self.engine.extract_text.assert_called_once_with(b'MOCK_PRIVATE_BYTES')
        self.assertFalse(Event.objects.exists())
        self.assertIn('LATEXFAUNA', RawSourceItemAdmin(RawSourceItem, admin.site).image_text(self.raw))
        self.assertIn('LATEXFAUNA', EventCandidateAdmin(EventCandidate, admin.site).raw_image_text(candidate))
        response = self.client.get('/api/events/')
        self.assertNotIn('MOCK_PRIVATE_BYTES', response.content.decode())
        self.assertNotIn('image_text', response.content.decode())
        self.assertNotIn(URL, response.content.decode())

    def test_dry_run_is_write_free(self):
        result = extract_raw_source_item(self.raw, dry_run=True)
        self.assertEqual(result.action, 'candidate')
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata, {'original': 'audit'})
        self.assertEqual(self.raw.extraction_status, 'new')
        self.assertFalse(EventCandidate.objects.exists())

    def test_failed_image_retains_caption_and_other_images(self):
        self.raw.raw_text = 'Концерт: Music\n12 жовтня 19:00'
        self.raw.raw_metadata['image_urls'] = [URL, URL + '?second', URL + '?third', URL + '?fourth']
        self.fetch.side_effect = [ImageTextError('image_http_failure'), b'good', b'good']
        result = extract_raw_source_item(self.raw)
        self.assertEqual(result.action, 'review')
        self.assertIn('title', result.unresolved[0]['reasons'])
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['image_text_status'], 'partial')
        self.assertEqual(self.raw.raw_metadata['image_text_images_attempted'], 3)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertIn('IMAGE 2:', self.raw.raw_metadata['image_text'])

    def test_all_images_fail_caption_still_extracts(self):
        self.raw.raw_text = 'Концерт: Music\n12 жовтня 19:00'
        self.fetch.side_effect = ImageTextError('image_http_failure')
        self.assertEqual(extract_raw_source_item(self.raw).action, 'created')
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['image_text_status'], 'failed')
        prepare_image_text(self.raw)
        self.fetch.assert_called_once()

    def test_caption_plus_poster_and_conflicts(self):
        self.raw.raw_text = 'Концерт: LATEXFAUNA\nАдреса: вул. Старознесенська, 24'
        self.raw.raw_metadata['image_text'] = POSTER
        result = extract_facts(self.raw)
        self.assertEqual(result.address, 'вул. Старознесенська, 24')
        self.assertEqual(result.price, Decimal('700'))
        self.assertEqual(result.start_time.day, 12)
        self.raw.raw_text += '\n13 жовтня\nЦіна: 500 грн'
        conflict = extract_facts(self.raw)
        self.assertIsNone(conflict.start_time)
        self.assertIsNone(conflict.price)
        self.assertLessEqual(conflict.confidence, .4)
        self.assertIn('Conflicting caption/image', conflict.extraction_notes)

    def test_missing_engine_reported_without_download(self):
        with patch('ingestion.image_text.configured_image_extractor', return_value=(None, 'ocr_engine_unavailable')):
            prepare_image_text(self.raw)
        self.assertEqual(self.raw.raw_metadata['image_text_status'], 'ocr_engine_unavailable')
        self.fetch.assert_not_called()


class RealRuntimePersistenceTests(TestCase):
    def test_real_sync_playwright_loop_stops_before_orm_and_next_source(self):
        SourceAccount.objects.create(username='first')
        SourceAccount.objects.create(username='second')
        runtimes = []
        def factory():
            browser = FakeBrowser()
            def start():
                runtime = sync_playwright().start()  # Real loop; no Chromium or network needed.
                runtimes.append(runtime)
                # Reproduce the precise Django safety failure inside the sync runtime.
                with self.assertRaises(SynchronousOnlyOperation):
                    RawSourceItem.objects.count()
                browser.close.side_effect = runtime.stop
                return browser
            browser.start.side_effect = start
            return browser
        try:
            with patch.dict('os.environ', {'INSTAGRAM_COLLECTION_PROVIDER': 'playwright'}), patch(
                'ingestion.providers.instagram_playwright.InstagramBrowser', side_effect=factory), patch(
                'ingestion.providers.instagram_playwright.time.sleep'), patch(
                'ingestion.management.commands.collect_instagram.time.sleep'):
                call_command('collect_instagram', limit=2, stdout=StringIO())
            self.assertEqual(RawSourceItem.objects.count(), 4)
            self.assertEqual(len(runtimes), 2)
            self.assertTrue(all(SourceAccount.objects.values_list('last_checked_at', flat=True)))
        finally:
            for runtime in runtimes:
                runtime.stop()
