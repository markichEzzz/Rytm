from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import SourceAccount, RawSourceItem, EventCandidate
from .providers.instagram import InstagramCollectionError, InstaloaderProvider, selected_provider
from .providers.instagram_browser import InstagramBrowser, SESSION_REQUIRED, profile_directory, session_authenticated
from .providers.instagram_html import normalize_post_html, recent_post_links, post_timestamp
from .providers.instagram_playwright import PlaywrightInstagramProvider


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    def count(self):
        if 'input[name=' in self.selector:
            return int(self.page.url.endswith('/accounts/login/'))
        if self.selector == 'main article':
            return int(self.page.authenticated)
        return 1

    def wait_for(self, **kwargs):
        return None

    def evaluate_all(self, expression, arg=None):
        if self.selector == 'a[href]':
            return ['/direct/inbox/', *self.page.links] if self.page.authenticated else ['/accounts/login/']
        if self.selector == 'header,h1,h2,[role="heading"]':
            return True
        if self.selector == 'time[datetime]':
            return [{'datetime': self.page.published.isoformat(), 'href': self.page.url}]
        return self.page.links

    def all_text_contents(self):
        return ['Private post caption']


class FakePage:
    def __init__(self):
        self.url = 'https://www.instagram.com/'
        self.authenticated = True
        self.links = ['/p/Good/', '/reel/Second/', '/p/Good/?duplicate=1']
        self.published = timezone.now() - timedelta(days=1)
        self.visits = []
        self.bad_posts = set()
        self.mouse = Mock()

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self.url = url
        if url in self.bad_posts:
            raise RuntimeError('PRIVATE_BROWSER_RESPONSE cookie=SECRET')
        return SimpleNamespace(status=200)

    def title(self):
        return 'Instagram'

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, *args):
        return None

    def get_by_role(self, *args, **kwargs):
        result = Mock()
        result.filter.return_value.count.return_value = 0
        return result

    def get_by_text(self, *args, **kwargs):
        return Mock(count=Mock(return_value=0))

    def locator(self, selector):
        return FakeLocator(self, selector)

    def content(self):
        return f'<meta property="og:url" content="{self.url}"><meta property="og:image" content="https://example.com/internal.jpg">'


class FakeBrowser:
    def __init__(self):
        self.page = FakePage()
        self.context = Mock()
        self.context.cookies.return_value = [{'name': 'sessionid', 'value': 'SECRET_COOKIE'}]
        self.start = Mock(return_value=self)
        self.close = Mock()


class HtmlAndBrowserTests(SimpleTestCase):
    def test_post_links_bounded_deduplicated_and_host_checked(self):
        links = ['/p/A/', '/p/A/?tracking=1', 'https://evil.example/p/B/', '/reel/B/', '/p/C/', '/direct/inbox/']
        self.assertEqual(recent_post_links(links, 2), ['https://www.instagram.com/p/A/', 'https://www.instagram.com/reel/B/'])

    def test_structured_metadata_mapping(self):
        payload = {'@context': 'https://schema.org', '@type': 'SocialMediaPosting',
                   'url': 'https://www.instagram.com/p/A/', 'articleBody': 'Caption facts',
                   'datePublished': '2026-10-07T19:00:00+03:00', 'image': 'https://example.com/private.jpg'}
        html = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
        item = normalize_post_html('https://www.instagram.com/p/A/', html, username='festrepublic')
        self.assertEqual(item.external_id, 'A')
        self.assertEqual(item.raw_text, 'Caption facts')
        self.assertTrue(timezone.is_aware(item.published_at))
        self.assertEqual(item.raw_image_url, 'https://example.com/private.jpg')
        self.assertEqual(item.raw_metadata['provider'], 'playwright')
        self.assertNotIn('html', item.raw_metadata)

    def test_structured_carousel_urls_are_bounded_primary_first(self):
        payload = {'@type': 'SocialMediaPosting', 'articleBody': 'Caption',
                   'image': [{'contentUrl': f'https://example.com/{index}.jpg'} for index in range(5)]}
        item = normalize_post_html('https://www.instagram.com/p/A/',
            '<script type="application/ld+json">' + json.dumps(payload) + '</script>')
        self.assertEqual(item.raw_image_url, 'https://example.com/0.jpg')
        self.assertEqual(item.raw_metadata['image_urls'], [f'https://example.com/{i}.jpg' for i in range(3)])

    def test_dom_fallback_and_missing_or_naive_timestamp(self):
        item = normalize_post_html('https://www.instagram.com/reel/B/', '<main></main>',
                                   caption='DOM caption', timestamp='2026-10-07T19:00:00')
        self.assertEqual(item.raw_text, 'DOM caption')
        self.assertIsNone(item.published_at)
        self.assertEqual(item.raw_metadata['post_kind'], 'reel')

    def test_timestamp_avoids_comment_dates_and_prefers_structured_metadata(self):
        records = [{'datetime': '2026-10-08T12:00:00Z', 'href': '/p/A/c/comment/'},
                   {'datetime': '2026-10-07T12:00:00Z', 'href': '/p/A/'}]
        self.assertEqual(post_timestamp(records, 'https://www.instagram.com/p/A/'), '2026-10-07T12:00:00Z')
        self.assertIsNone(post_timestamp([{'datetime': 'one'}, {'datetime': 'two'}], 'https://www.instagram.com/p/A/'))
        html = '<meta property="article:published_time" content="2026-10-07T12:00:00Z"><meta property="og:description" content="caption">'
        item = normalize_post_html('https://www.instagram.com/p/A/', html, timestamp='2026-10-08T12:00:00Z')
        self.assertEqual(item.published_at.day, 7)

    def test_metadata_missing_and_canonical_mismatch_raise(self):
        for html in ('<main>Loading</main>', '<meta property="og:url" content="https://www.instagram.com/p/OTHER/">'):
            with self.assertRaises(InstagramCollectionError):
                normalize_post_html('https://www.instagram.com/p/A/', html)

    def test_profile_default_external_and_repo_configuration_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            path = profile_directory()
            self.assertEqual(path, Path.home() / '.local/share/rytm-instagram-profile')
            self.assertNotIn(Path(settings.BASE_DIR), path.parents)
        for path in (str(settings.BASE_DIR / '.instagram-playwright'), str(Path(settings.BASE_DIR).parent / 'profile'), 'relative-profile'):
            with patch.dict(os.environ, {'INSTAGRAM_PLAYWRIGHT_PROFILE_DIR': path}), self.assertRaises(InstagramCollectionError):
                profile_directory()
        with TemporaryDirectory() as folder:
            repo = Path(folder) / 'repo'
            repo.mkdir()
            (repo / '.git').touch()
            with patch.dict(os.environ, {'INSTAGRAM_PLAYWRIGHT_PROFILE_DIR': str(repo / 'private')}), self.assertRaises(InstagramCollectionError):
                profile_directory()

    def test_session_needs_cookie_and_navigation_not_redirect(self):
        browser = FakeBrowser()
        self.assertTrue(session_authenticated(browser.context, browser.page))
        browser.context.cookies.return_value = []
        self.assertFalse(session_authenticated(browser.context, browser.page))
        browser.context.cookies.return_value = [{'name': 'sessionid', 'value': 'SECRET'}]
        browser.page.url = 'https://www.instagram.com/accounts/login/'
        self.assertFalse(session_authenticated(browser.context, browser.page))
        browser.page.url = 'https://www.instagram.com/challenge/'
        self.assertFalse(session_authenticated(browser.context, browser.page))

    def test_launch_uses_persistent_profile_and_login_always_headed(self):
        with TemporaryDirectory() as profile, patch.dict(os.environ, {
            'INSTAGRAM_PLAYWRIGHT_PROFILE_DIR': profile, 'INSTAGRAM_PLAYWRIGHT_HEADLESS': 'true',
        }), patch('ingestion.providers.instagram_browser.profile_directory', return_value=Path(profile)), \
                patch('ingestion.providers.instagram_browser.sync_playwright') as factory:
            # Launch configuration is independent of Git ancestors of the host's
            # temporary directory; profile safety is covered separately above.
            runtime = factory.return_value.start.return_value
            context = runtime.chromium.launch_persistent_context.return_value
            context.pages = [Mock()]
            browser = InstagramBrowser(headed_login=True).start()
            kwargs = runtime.chromium.launch_persistent_context.call_args.kwargs
            self.assertEqual(kwargs['user_data_dir'], profile)
            self.assertFalse(kwargs['headless'])
            self.assertFalse(kwargs['accept_downloads'])
            browser.close()
            context.close.assert_called_once()
            runtime.stop.assert_called_once()

    def test_selection_default_and_explicit_no_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(selected_provider(), InstaloaderProvider)
        with patch.dict(os.environ, {'INSTAGRAM_COLLECTION_PROVIDER': 'playwright'}):
            self.assertIsInstance(selected_provider(), PlaywrightInstagramProvider)
        with patch.dict(os.environ, {'INSTAGRAM_COLLECTION_PROVIDER': 'unknown'}), self.assertRaises(InstagramCollectionError):
            selected_provider()

    def test_unauthenticated_session_clear_error_and_no_posts(self):
        browser = FakeBrowser()
        browser.context.cookies.return_value = []
        provider = PlaywrightInstagramProvider(browser_factory=lambda: browser)
        with patch('ingestion.providers.instagram_playwright.time.sleep'):
            with self.assertRaisesMessage(InstagramCollectionError, SESSION_REQUIRED):
                provider.fetch_recent('festrepublic', 3)
        self.assertEqual(len(browser.page.visits), 1)
        provider.close()
        browser.close.assert_called_once()

    def test_per_post_failure_continues_and_no_secret_logs(self):
        browser = FakeBrowser()
        browser.page.bad_posts.add('https://www.instagram.com/p/Good/')
        provider = PlaywrightInstagramProvider(browser_factory=lambda: browser)
        with patch('ingestion.providers.instagram_playwright.time.sleep'), self.assertLogs('ingestion.providers.instagram_playwright', level='WARNING') as logs:
            items = provider.fetch_recent('festrepublic', 3)
        self.assertEqual([item.external_id for item in items], ['Second'])
        self.assertNotIn('SECRET', str(logs.output))
        self.assertEqual(len(browser.page.visits), 3)
        provider.close()

    def test_all_failed_posts_fail_clearly(self):
        browser = FakeBrowser()
        browser.page.bad_posts.update(['https://www.instagram.com/p/Good/', 'https://www.instagram.com/reel/Second/'])
        with patch('ingestion.providers.instagram_playwright.time.sleep'):
            with self.assertRaisesMessage(InstagramCollectionError, 'usable structures'):
                PlaywrightInstagramProvider(browser_factory=lambda: browser).fetch_recent('festrepublic', 2)

    def test_rate_block_has_no_retry(self):
        browser = FakeBrowser()
        browser.page.goto = Mock(return_value=SimpleNamespace(status=429))
        provider = PlaywrightInstagramProvider(browser_factory=lambda: browser)
        with patch('ingestion.providers.instagram_playwright.time.sleep'):
            for username in ('one', 'two'):
                with self.assertRaisesMessage(InstagramCollectionError, 'rate limited'):
                    provider.fetch_recent(username, 3)
        browser.page.goto.assert_called_once()

    def test_login_manual_confirmation_success_and_cleanup(self):
        browser = FakeBrowser()
        output = StringIO()
        with patch('ingestion.management.commands.instagram_browser_login.InstagramBrowser', return_value=browser) as factory, patch('builtins.input', return_value='') as confirm:
            call_command('instagram_browser_login', stdout=output)
        factory.assert_called_once_with(headed_login=True)
        confirm.assert_called_once_with()
        browser.close.assert_called_once()
        self.assertIn('manually', output.getvalue())
        self.assertIn('appears authenticated', output.getvalue())
        self.assertNotIn('SECRET', output.getvalue())

    def test_login_unconfirmed_or_unauthenticated_closes(self):
        for cause in ('unauthenticated', 'eof'):
            browser = FakeBrowser()
            if cause == 'unauthenticated':
                browser.context.cookies.return_value = []
            with patch('ingestion.management.commands.instagram_browser_login.InstagramBrowser', return_value=browser), patch('builtins.input', side_effect=EOFError if cause == 'eof' else None):
                with self.assertRaises(CommandError):
                    call_command('instagram_browser_login', stdout=StringIO())
            browser.close.assert_called_once()


class PlaywrightCommandTests(TestCase):
    def setUp(self):
        self.source = SourceAccount.objects.create(username='festrepublic')
        self.browser = FakeBrowser()
        provider = patch('ingestion.providers.instagram_playwright.InstagramBrowser', return_value=self.browser)
        provider.start()
        self.addCleanup(provider.stop)
        env = patch.dict(os.environ, {'INSTAGRAM_COLLECTION_PROVIDER': 'playwright'})
        env.start()
        self.addCleanup(env.stop)
        delay = patch('ingestion.providers.instagram_playwright.time.sleep')
        delay.start()
        self.addCleanup(delay.stop)

    def test_real_command_normalization_dedup_and_private_only(self):
        call_command('collect_instagram', username='festrepublic', limit=2, stdout=StringIO())
        call_command('collect_instagram', username='festrepublic', limit=2, stdout=StringIO())
        self.assertEqual(RawSourceItem.objects.count(), 2)
        self.assertEqual(set(RawSourceItem.objects.values_list('external_id', flat=True)), {'Good', 'Second'})
        self.assertTrue(all(item.extraction_status == 'new' for item in RawSourceItem.objects.all()))
        self.assertFalse(EventCandidate.objects.exists())
        from events.models import Event
        self.assertFalse(Event.objects.exists())

    def test_dry_run_does_not_persist(self):
        call_command('collect_instagram', username='festrepublic', dry_run=True, limit=2, stdout=StringIO())
        self.assertFalse(RawSourceItem.objects.exists())
        self.source.refresh_from_db()
        self.assertIsNone(self.source.last_checked_at)

    def test_disabled_source_skipped_without_browser(self):
        self.source.enabled = False
        self.source.save()
        call_command('collect_instagram', stdout=StringIO())
        self.browser.start.assert_not_called()
        self.assertFalse(RawSourceItem.objects.exists())
