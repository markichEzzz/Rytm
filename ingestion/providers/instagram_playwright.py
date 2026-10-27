"""Read-only, bounded collection using a manually authenticated persistent profile."""
from datetime import timedelta
import logging
import json
import re
import time

from django.utils import timezone

from .instagram import InstagramCollectionError, MAX_LIMIT, RECENT_DAYS
from .instagram_browser import InstagramBrowser, SESSION_REQUIRED, blocked_page, session_authenticated, has_session_cookie
from .instagram_html import normalize_post_html, post_identity, post_timestamp
from .instagram_discovery import discover_profile_posts, empty_profile_error, profile_diagnostics

logger = logging.getLogger(__name__)
ACTION_DELAY_SECONDS = 1


class PlaywrightInstagramProvider:
    def __init__(self, browser_factory=None):
        self.browser_factory = browser_factory or InstagramBrowser
        self.browser = None
        self.unavailable = None
        self.diagnostic_sink = None

    def set_diagnostics(self, sink):
        self.diagnostic_sink = sink

    def _diagnostics(self, data):
        if self.diagnostic_sink:
            self.diagnostic_sink(json.dumps(data, ensure_ascii=False))

    def _browser(self):
        if self.unavailable:
            raise InstagramCollectionError(self.unavailable)
        if self.browser is None:
            self.browser = self.browser_factory().start()
            # Pages need text/metadata, not media files. This is resource conservation, not evasion.
            self.browser.context.route('**/*', lambda route: route.abort() if route.request.resource_type
                                       in ('image', 'media', 'font') else route.continue_())
        return self.browser

    def _navigate(self, browser, url):
        time.sleep(ACTION_DELAY_SECONDS)
        response = browser.page.goto(url, wait_until='domcontentloaded')
        if response and response.status in (400, 401, 403, 429):
            self.unavailable = 'Instagram browser access is blocked or rate limited; no retry was made. Complete verification manually if requested.'
            raise InstagramCollectionError(self.unavailable)
        if response and response.status == 404:
            raise InstagramCollectionError('Instagram page was not found; no retry was made.')
        if blocked_page(browser.page) or not has_session_cookie(browser.context):
            self.unavailable = SESSION_REQUIRED
            raise InstagramCollectionError(SESSION_REQUIRED)
        # Wait for semantic page content, without scrolling or interacting with account controls.
        browser.page.locator('main, article, [role="main"]').first.wait_for(state='attached', timeout=10000)
        try:
            browser.page.locator('a[href*="/direct/"], a[href*="/accounts/edit"], a[href*="/accounts/activity"], a[href*="/your_activity"]').first.wait_for(state='attached', timeout=10000)
        except Exception:
            self.unavailable = SESSION_REQUIRED
            raise InstagramCollectionError(SESSION_REQUIRED) from None
        if not session_authenticated(browser.context, browser.page):
            self.unavailable = SESSION_REQUIRED
            raise InstagramCollectionError(SESSION_REQUIRED)

    def fetch_recent(self, username, limit):
        if not re.fullmatch(r'[A-Za-z0-9_.]+', username) or not 1 <= limit <= MAX_LIMIT:
            raise InstagramCollectionError('Instagram source username or post limit is invalid.')
        try:
            browser = self._browser()
            self._navigate(browser, f'https://www.instagram.com/{username}/')
            try:
                posts = discover_profile_posts(browser.page, username, limit)
            finally:
                if self.diagnostic_sink:
                    try:
                        self._diagnostics(profile_diagnostics(browser.page, username))
                    except Exception:
                        self._diagnostics({'source_username': username, 'diagnostics': 'DOM diagnostics unavailable.'})
            if not posts:
                raise InstagramCollectionError(empty_profile_error(browser.page, username))
            cutoff = timezone.now() - timedelta(days=RECENT_DAYS)
            items, parsed_count = [], 0
            for url in posts:
                try:
                    self._navigate(browser, url)
                    current = post_identity(browser.page.url)
                    if not current or current[0] != post_identity(url)[0]:
                        raise InstagramCollectionError('Instagram did not open the requested post.')
                    captions = browser.page.locator('main h1, article h1').all_text_contents()
                    timestamps = browser.page.locator('time[datetime]').evaluate_all(
                        '(elements) => elements.map(e => ({datetime: e.getAttribute("datetime"), href: e.closest("a")?.getAttribute("href")}))'
                    )
                    item = normalize_post_html(
                        url, browser.page.content(), caption=captions[0] if captions else '',
                        timestamp=post_timestamp(timestamps, url), username=username,
                    )
                    parsed_count += 1
                    self._diagnostics({'shortcode': item.external_id, 'post_kind': item.raw_metadata['post_kind'],
                                       'caption_available': bool(item.raw_text),
                                       'published_at_available': item.published_at is not None,
                                       'private_image_url_available': bool(item.raw_image_url)})
                    if item.published_at is None or item.published_at >= cutoff:
                        items.append(item)
                except Exception:
                    if self.unavailable:
                        raise InstagramCollectionError(self.unavailable) from None
                    # A removed/malformed individual post must not stop other bounded links.
                    logger.warning('Instagram browser post %s could not be read; skipped without retry.', post_identity(url)[0])
            self._diagnostics({'source_username': username, 'inspected_links': len(posts),
                               'normalized_items': parsed_count, 'recent_items': len(items)})
            if parsed_count == 0:
                raise InstagramCollectionError('None of the bounded Instagram posts had usable structures; no retry was made.')
            return items
        except InstagramCollectionError:
            raise
        except Exception:
            raise InstagramCollectionError('Instagram DOM discovery/parsing failed; use --debug-browser and check expected page structures. No retry was made.') from None
        finally:
            # sync_playwright keeps an asyncio loop running in this thread until stop().
            # End its lifetime before callers resume synchronous ORM/transactions.
            self.close()

    def close(self):
        if self.browser:
            self.browser.close()
            self.browser = None
