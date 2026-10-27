from html.parser import HTMLParser
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase

from .providers.instagram import InstagramCollectionError
from .providers.instagram_discovery import discover_profile_posts, dismiss_harmless_overlays, empty_profile_error, safe_page_url
from .providers.instagram_html import recent_post_links, post_identity
from . import test_instagram_playwright as browser_tests
from .test_instagram_playwright import FakePage


class AnchorFixture(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.hrefs = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        if tag == 'a':
            self.hrefs.append(dict(attributes).get('href'))


class DiscoveryTests(SimpleTestCase):
    def test_html_link_fixtures(self):
        fixtures = (
            ('<a href="/p/P/">post</a>', ['https://www.instagram.com/p/P/']),
            ('<a href="/reel/R/">reel</a>', ['https://www.instagram.com/reel/R/']),
            ('<div role="main"><a href="/festrepublic/p/P/"></a><a href="https://www.instagram.com/festrepublic/reel/R/?igsh=PRIVATE"></a></div>',
             ['https://www.instagram.com/p/P/', 'https://www.instagram.com/reel/R/']),
            ('<a href="/p/P/"></a><a href="/festrepublic/p/P/?tracking=1"></a>', ['https://www.instagram.com/p/P/']),
            ('<a href="https://www.instagram.com/tv/T/"></a>', ['https://www.instagram.com/tv/T/']),
            ('<header>empty profile</header>', []),
            ('<a href="/another/reel/R/"></a><a href="https://evil.example/p/P/"></a>', []),
        )
        for html, expected in fixtures:
            with self.subTest(html=html):
                self.assertEqual(recent_post_links(AnchorFixture(html).hrefs, 3, username='festrepublic'), expected)

    def test_username_prefixed_identity_and_safe_queries(self):
        self.assertEqual(post_identity('https://www.instagram.com/festrepublic/reel/ABC/?igsh=SECRET'),
                         ('ABC', 'https://www.instagram.com/reel/ABC/'))
        self.assertEqual(safe_page_url('https://www.instagram.com/festrepublic/?token=SECRET'), 'https://www.instagram.com/festrepublic/')
        self.assertNotIn('SECRET', safe_page_url('https://www.instagram.com/challenge/SECRET/?token=SECRET'))

    def test_lazy_links_discovered_after_one_small_scroll(self):
        page = FakePage()
        page.links = []
        page.mouse.wheel.side_effect = lambda *args: page.links.extend(['/festrepublic/reel/Lazy/'])
        self.assertEqual(discover_profile_posts(page, 'festrepublic', 3), ['https://www.instagram.com/reel/Lazy/'])
        page.mouse.wheel.assert_called_once_with(0, 650)

    def test_empty_discovery_stops_after_two_scrolls_and_does_not_claim_private(self):
        page = FakePage()
        page.links = []
        self.assertEqual(discover_profile_posts(page, 'festrepublic', 3), [])
        self.assertEqual(page.mouse.wheel.call_count, 2)
        error = empty_profile_error(page, 'festrepublic')
        self.assertIn('loaded but no post/reel links', error)
        self.assertNotIn('private', error)

    def test_security_page_never_dismissed_or_scrolled(self):
        page = FakePage()
        page.url = 'https://www.instagram.com/challenge/'
        with self.assertRaises(InstagramCollectionError):
            discover_profile_posts(page, 'festrepublic', 3)
        page.mouse.wheel.assert_not_called()

    def test_only_identified_harmless_notification_dialog_dismissed(self):
        page = FakePage()
        dialog = Mock()
        dialog.count.return_value = 1
        dialog.first.is_visible.return_value = True
        dialog.first.get_by_text.return_value.count.return_value = 0
        button = dialog.first.get_by_role.return_value
        button.count.return_value = 1
        button.is_visible.return_value = True
        absent = Mock()
        absent.count.return_value = 0
        page.get_by_role = Mock(return_value=Mock(filter=Mock(side_effect=[absent, dialog])))
        dismiss_harmless_overlays(page)
        button.click.assert_called_once_with(timeout=2000)

    def test_verification_dialog_never_clicked(self):
        page = FakePage()
        dialog = Mock()
        dialog.count.return_value = 1
        dialog.first.is_visible.return_value = True
        dialog.first.get_by_text.return_value.count.return_value = 1
        page.get_by_role = Mock(return_value=Mock(filter=Mock(return_value=dialog)))
        dismiss_harmless_overlays(page)
        dialog.first.get_by_role.assert_not_called()


class DebugBrowserCommandTests(TestCase):
    def setUp(self):
        browser_tests.PlaywrightCommandTests.setUp(self)

    # Existing command integration setup uses mock browsers and a private test database.
    def test_debug_output_sanitized_and_dry_run_unchanged(self):
        self.browser.page.links = ['/festrepublic/p/Good/?igsh=SECRET', '/festrepublic/reel/Second/']
        output = StringIO()
        call_command('collect_instagram', username='festrepublic', limit=3, dry_run=True,
                     debug_browser=True, stdout=output)
        self.assertIn('"post_links": 1', output.getvalue())
        self.assertIn('"reel_links": 1', output.getvalue())
        self.assertIn('"normalized_items": 2', output.getvalue())
        self.assertNotIn('SECRET', output.getvalue())
        from .models import RawSourceItem
        self.assertFalse(RawSourceItem.objects.exists())
