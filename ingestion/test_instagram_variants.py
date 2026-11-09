"""Offline fixtures only: no Instagram, live browser navigation or production seeding."""
from datetime import datetime
from io import StringIO
import json
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .instagram_urls import canonical_profile_url
from .instagram_test_support import isolate_instagram_schedule
from .management.commands.seed_source_accounts import OBSOLETE_BUNDLED
from .models import SourceAccount, RawSourceItem
from .providers.instagram import InstagramCollectionError
from .providers.instagram_html import normalize_post_html, post_identity, recent_post_links, post_timestamp
from .providers.instagram_playwright import PlaywrightInstagramProvider
from .test_instagram_playwright import FakeBrowser

CURRENT = [
    'festrepublic', 'malevich_concert_arena', 'kotelnya.ruin.place', 'kotelnya_',
    'cult_comedy_hall', 'rumbambar.lviv', 'emily_resort', 'lollipop_projectsss',
    'lvivart.center', 'dzyga.gallery', 'les_kurbas_theatre', 'urbancamp.lviv',
    'center.for.urban.history', 'mad.lviv', 'pravdabeer', 'zankovetska.theatre',
]
CAPTION = 'Концерт: Гурт Ритм\n15 листопада 2026 19:00\nАдреса: вул. Тестова, 1\nВід 150 грн'


def browser_fixture(html='', *, caption='', dialog=False, timestamps=True):
    browser = FakeBrowser()
    browser.page.content = lambda: html
    locator = browser.page.locator
    def locate(selector):
        result = locator(selector)
        selected = '[role="dialog"] h1' in selector if dialog else selector.startswith('article h1')
        result.all_text_contents = Mock(return_value=[caption] if selected and caption else [])
        if selector == 'time[datetime]' and not timestamps:
            result.evaluate_all = Mock(return_value=[])
        return result
    browser.page.locator = locate
    return browser


class CanonicalProfileTests(SimpleTestCase):
    def test_profile_urls_discard_every_query_and_fragment(self):
        for username in CURRENT:
            for key in ('obrf', 'cplk', 'srtk', 'rpxt', 'mdxt', 'vrfl', 'psln', 'xtok', 'exln'):
                value = f'https://www.instagram.com/{username}/?{key}=TRANSIENT#fragment'
                self.assertEqual(canonical_profile_url(value), f'https://www.instagram.com/{username}/')
        self.assertEqual(canonical_profile_url(' @FESTREPUBLIC '), 'https://www.instagram.com/festrepublic/')

    def test_profile_normalization_rejects_foreign_and_content_urls(self):
        for value in ('https://evil.example/festrepublic/', 'https://www.instagram.com/p/Code/',
                      'https://user:secret@www.instagram.com/name/', 'https://www.instagram.com:444/name/'):
            with self.assertRaises(ValueError):
                canonical_profile_url(value)


class SourceSeedTests(TestCase):
    def test_exact_current_bundle_is_idempotent_and_canonical(self):
        for _ in range(2):
            call_command('seed_source_accounts', stdout=StringIO())
        self.assertEqual(set(SourceAccount.objects.values_list('username', flat=True)), set(CURRENT))
        self.assertEqual(SourceAccount.objects.count(), 16)
        self.assertTrue(SourceAccount.objects.get(username='cult_comedy_hall').enabled)
        self.assertFalse(SourceAccount.objects.filter(username='cult.comedy.hall').exists())
        for source in SourceAccount.objects.all():
            self.assertEqual(source.source_url, f'https://www.instagram.com/{source.username}/')

    def test_preserves_settings_and_history_but_normalizes_matching_url(self):
        checked = timezone.now()
        source = SourceAccount.objects.create(username='festrepublic', enabled=False, display_name='Operator name',
            last_checked_at=checked, source_url='https://instagram.com/festrepublic/?xtok=TRANSIENT#fragment')
        raw = RawSourceItem.objects.create(source_account=source, external_id='old', source_url='https://www.instagram.com/p/Old/')
        call_command('seed_source_accounts', stdout=StringIO())
        source.refresh_from_db()
        self.assertFalse(source.enabled)
        self.assertEqual(source.display_name, 'Operator name')
        self.assertEqual(source.last_checked_at, checked)
        self.assertEqual(source.source_url, 'https://www.instagram.com/festrepublic/')
        self.assertEqual(RawSourceItem.objects.get(pk=raw.pk).source_account_id, source.pk)

    def test_deactivates_only_old_bundle_without_deleting_or_renaming(self):
        for username in OBSOLETE_BUNDLED:
            SourceAccount.objects.create(username=username)
        old = SourceAccount.objects.get(username='cult.comedy.hall')
        raw = RawSourceItem.objects.create(source_account=old, external_id='historic', source_url='https://www.instagram.com/p/Old/')
        custom = SourceAccount.objects.create(username='operator_added')
        telegram = SourceAccount.objects.create(username='cult.comedy.hall', platform='telegram')
        call_command('seed_source_accounts', stdout=StringIO())
        self.assertFalse(SourceAccount.objects.filter(username__in=OBSOLETE_BUNDLED, platform='instagram', enabled=True).exists())
        self.assertTrue(SourceAccount.objects.get(pk=custom.pk).enabled)
        self.assertTrue(SourceAccount.objects.get(pk=telegram.pk).enabled)
        self.assertEqual(RawSourceItem.objects.get(pk=raw.pk).source_account_id, old.pk)
        self.assertTrue(SourceAccount.objects.filter(pk=old.pk, username='cult.comedy.hall').exists())

    def test_seed_dry_run_has_no_writes(self):
        old = SourceAccount.objects.create(username='cult.comedy.hall')
        before = list(SourceAccount.objects.values())
        call_command('seed_source_accounts', dry_run=True, stdout=StringIO())
        self.assertEqual(list(SourceAccount.objects.values()), before)
        self.assertTrue(SourceAccount.objects.get(pk=old.pk).enabled)


class PostVariantTests(SimpleTestCase):
    def test_permalink_canonicalization_and_rejected_anchors(self):
        for path, expected in [('/p/Ab_1', '/p/Ab_1/'), ('/p/Ab_1/?img_index=2#more', '/p/Ab_1/'),
                               ('/reel/Reel_2/?sharing=PRIVATE', '/reel/Reel_2/'),
                               ('/reels/Reel_2', '/reel/Reel_2/'), ('/coauthor/reel/Reel_2/', '/reel/Reel_2/')]:
            self.assertEqual(post_identity(path)[1], 'https://www.instagram.com' + expected)
        for path in ('/tv/Old/', '/explore/', '/explore/tags/music/', '/reels/audio/123/', '/name/tagged/',
                     '/name/', '/p/Code/comments/', 'https://evil.example/p/Code/'):
            self.assertIsNone(post_identity(path))

    def test_collaboration_links_allowed_only_in_scoped_discovery(self):
        links = ['/coauthor/p/Collab/?share=PRIVATE', '/p/Pinned/', '/reel/Video/']
        self.assertEqual(len(recent_post_links(links, 3, username='source')), 2)
        self.assertEqual(recent_post_links(links, 1, username='source', allow_collaborations=True),
                         ['https://www.instagram.com/p/Collab/'])

    def test_comment_timestamp_is_not_used_as_post_publication(self):
        self.assertIsNone(post_timestamp([{'href': '/p/A/c/123/', 'datetime': '2026-11-08T18:00:00Z'}], '/p/A/'))

    def test_caption_only_with_generic_page_canonical_and_missing_media(self):
        html = '<meta property="og:url" content="https://www.instagram.com/account/">'
        item = normalize_post_html('/p/A/', html, caption=CAPTION, timestamp='2026-11-08T18:00:00Z', username='account')
        self.assertEqual(item.raw_text, CAPTION)
        self.assertEqual(item.raw_image_url, '')
        self.assertEqual(item.raw_metadata['image_urls'], [])
        self.assertIsNotNone(item.published_at)

    def test_video_schema_and_standard_description_metadata(self):
        payload = {'@type': 'VideoObject', 'url': 'https://www.instagram.com/reels/R/',
                   'description': CAPTION, 'uploadDate': '2026-11-08T18:00:00Z'}
        item = normalize_post_html('/reel/R/', '<script type="application/ld+json">' + json.dumps(payload) + '</script>')
        self.assertEqual(item.raw_text, CAPTION)
        self.assertEqual(item.raw_metadata['post_kind'], 'reel')
        self.assertEqual(item.raw_image_url, '')
        item = normalize_post_html('/p/P/', '<meta name="description" content="Useful caption">')
        self.assertEqual(item.raw_text, 'Useful caption')

    def test_invalid_optional_media_does_not_erase_caption(self):
        for url in ('javascript:PRIVATE', 'https://broken[/', 'https://example.com/' + 'x' * 2100):
            item = normalize_post_html('/p/A/', f'<meta property="og:image" content="{url}">', caption=CAPTION)
            self.assertEqual(item.raw_text, CAPTION)
            self.assertEqual(item.raw_image_url, '')

    def test_existing_extractor_uses_caption_without_media_or_ocr(self):
        from .extraction import extract_facts
        from .image_text import prepare_image_text
        for html in ('', '<meta property="og:image" content="https://example.com/irrelevant.jpg">'):
            with self.subTest(media=bool(html)):
                item = normalize_post_html('/p/A/', html, caption=CAPTION, username='rumbambar.lviv')
                raw = RawSourceItem(source_account=SourceAccount(username='rumbambar.lviv'),
                    external_id=item.external_id, source_url=item.source_url, raw_text=item.raw_text,
                    raw_image_url=item.raw_image_url, raw_metadata=item.raw_metadata)
                engine = Mock(spec=['extract_text'])
                engine.extract_text.return_value = ''
                prepare_image_text(raw, extractor=engine, downloader=lambda url: b'OFFLINE_BYTES')
                with patch('django.utils.timezone.now', return_value=datetime.fromisoformat('2026-11-08T12:00:00+02:00')):
                    result = extract_facts(raw)
                self.assertTrue(result.is_event)
                self.assertEqual(result.title, 'Гурт Ритм')
                self.assertEqual(result.price, 150)
                self.assertEqual(raw.raw_text, CAPTION)
                self.assertFalse(raw.raw_metadata.get('image_text'))

    def test_caption_precedes_carousel_description_without_losing_media(self):
        payload = {'@type': 'SocialMediaPosting', 'articleBody': 'Generic image caption',
                   'image': ['https://example.com/one.jpg', 'https://example.com/two.jpg']}
        item = normalize_post_html('/p/A/', '<script type="application/ld+json">' + json.dumps(payload) + '</script>', caption=CAPTION)
        self.assertEqual(item.raw_text, CAPTION)
        self.assertEqual(len(item.raw_metadata['image_urls']), 2)


class ProviderVariantTests(SimpleTestCase):
    def setUp(self):
        isolate_instagram_schedule(self)
        sleeper = patch('ingestion.providers.instagram_playwright.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_dialog_caption_and_same_shortcode_post_to_reel_redirect(self):
        browser = browser_fixture(caption=CAPTION, dialog=True)
        browser.page.links = ['/p/Good/']
        original = browser.page.goto
        def goto(url, **kwargs):
            result = original(url, **kwargs)
            if '/p/Good/' in url:
                browser.page.url = 'https://www.instagram.com/reels/Good/?img_index=1'
            return result
        browser.page.goto = goto
        items = PlaywrightInstagramProvider(browser_factory=lambda: browser).fetch_recent('configured_source', 1)
        self.assertEqual(items[0].source_url, 'https://www.instagram.com/reel/Good/')
        self.assertEqual(items[0].raw_text, CAPTION)
        self.assertEqual(items[0].raw_metadata['source_username'], 'configured_source')
        self.assertEqual(items[0].raw_metadata['post_kind'], 'reel')

    def test_all_unsupported_posts_have_safe_presence_diagnostics(self):
        browser = browser_fixture('<main>Unsupported layout</main>')
        output = StringIO()
        provider = PlaywrightInstagramProvider(browser_factory=lambda: browser)
        provider.set_diagnostics(output.write)
        # Mismatched real post canonical must still be refused, even with a caption.
        browser.page.content = lambda: '<meta property="og:url" content="https://www.instagram.com/p/Other/?secret=PRIVATE">'
        items = provider.fetch_recent('one', 2)
        self.assertEqual(items.collection_status, 'no_parseable_posts')
        self.assertEqual(items.skipped_posts, 2)
        self.assertEqual(items, [])
        self.assertIn('caption_candidate_found', output.getvalue())
        self.assertIn('path_class', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())
        # An empty layout also has no usable publication/caption metadata.
        empty = browser_fixture('<main>Unrecognized post</main>', timestamps=False)
        empty_items = PlaywrightInstagramProvider(browser_factory=lambda: empty).fetch_recent('one', 1)
        self.assertEqual(empty_items.collection_status, 'no_parseable_posts')
        self.assertEqual(empty_items.skipped_posts, 1)

    def test_broken_post_then_valid_caption_post_is_partial_success(self):
        browser = browser_fixture(caption=CAPTION)
        browser.page.bad_posts.add('https://www.instagram.com/p/Good/')
        items = PlaywrightInstagramProvider(browser_factory=lambda: browser).fetch_recent('one', 2)
        self.assertEqual(items.collection_status, 'ok')
        self.assertEqual(items.skipped_posts, 1)
        self.assertEqual(items[0].raw_text, CAPTION)


class CaptionPipelineTests(TestCase):
    def setUp(self):
        isolate_instagram_schedule(self)
        self.source = SourceAccount.objects.create(username='rumbambar.lviv')
        self.browser = browser_fixture(caption=CAPTION)
        factory = patch('ingestion.providers.instagram_playwright.InstagramBrowser', return_value=self.browser)
        factory.start()
        self.addCleanup(factory.stop)
        sleeper = patch('ingestion.providers.instagram_playwright.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No live requests permitted'))
        network.start()
        self.addCleanup(network.stop)

    def collect(self):
        output = StringIO()
        call_command('collect_instagram', '--source', self.source.username, '--provider', 'playwright', '--limit-posts', '1', stdout=output)
        return output.getvalue()

    def test_caption_without_image_reaches_existing_extractor(self):
        from .extraction import extract_raw_source_item
        self.collect()
        raw = RawSourceItem.objects.get()
        self.assertEqual(raw.source_account, self.source)
        self.assertEqual(raw.raw_text, CAPTION)
        self.assertEqual(raw.raw_image_url, '')
        with patch('django.utils.timezone.now', return_value=datetime.fromisoformat('2026-11-08T12:00:00+02:00')):
            outcome = extract_raw_source_item(raw)
        self.assertEqual(outcome.action, 'created')
        self.assertEqual(outcome.candidate.title, 'Гурт Ритм')
        self.assertEqual(outcome.candidate.price, 150)
        self.assertEqual(outcome.candidate.status, 'new')

    def test_uninformative_image_does_not_override_useful_caption(self):
        from .extraction import extract_raw_source_item
        self.browser.page.content = lambda: '<meta property="og:image" content="https://example.com/irrelevant.jpg">'
        self.collect()
        raw = RawSourceItem.objects.get()
        engine = Mock(spec=['extract_text'])
        engine.extract_text.return_value = ''
        with patch('django.utils.timezone.now', return_value=datetime.fromisoformat('2026-11-08T12:00:00+02:00')), patch(
            'ingestion.image_text.configured_image_extractor', return_value=(engine, '')), patch(
            'ingestion.image_text.download_image', return_value=b'OFFLINE_BYTES'):
            outcome = extract_raw_source_item(raw)
        self.assertEqual(outcome.candidate.title, 'Гурт Ритм')
        self.assertEqual(raw.raw_text, CAPTION)

    def test_all_unsupported_window_is_not_failure_or_no_new_content(self):
        self.browser = browser_fixture('<main>Unrecognized post</main>', timestamps=False)
        with patch('ingestion.providers.instagram_playwright.InstagramBrowser', return_value=self.browser):
            output = self.collect()
        self.assertIn('Instagram status: no_parseable_posts', output)
        self.assertIn('Failures: 0', output)
        self.assertIn('No new posts: 0', output)
        self.assertIn('Posts skipped: 1', output)
        self.assertFalse(RawSourceItem.objects.exists())
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.last_checked_at)
