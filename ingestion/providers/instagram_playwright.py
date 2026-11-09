"""Read-only, low-volume collection using a manually authenticated profile."""
from datetime import timedelta
import logging
import json
import re
import time
from urllib.parse import urlparse

from django.utils import timezone
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from ingestion.instagram_policy import InstagramPolicy
from .instagram import InstagramCollectionError, InstagramPostItems, MAX_LIMIT, RECENT_DAYS
from ingestion.instagram_urls import canonical_profile_url
from .instagram_browser import InstagramBrowser, session_authenticated, has_session_cookie
from .instagram_html import normalize_post_html, post_identity, post_timestamp, post_dom_candidates, post_candidate_diagnostics
from .instagram_discovery import discover_profile_posts, profile_diagnostics, failure_diagnostics
from .instagram_safety import page_failure, failure

logger = logging.getLogger(__name__)


class PlaywrightInstagramProvider:
    def __init__(self, browser_factory=None):
        self.browser_factory = browser_factory or InstagramBrowser
        self.browser = None
        self.unavailable = None
        self.diagnostic_sink = None
        self.restriction = None
        self.stage = 'browser_launch'
        self.navigation_status = None

    def set_diagnostics(self, sink):
        self.diagnostic_sink = sink

    def _diagnostics(self, data):
        if self.diagnostic_sink:
            try:
                self.diagnostic_sink(json.dumps(data, ensure_ascii=False))
            except Exception:
                # Diagnostic I/O must never mask a security refusal or resume collection.
                logger.warning('Instagram sanitized diagnostic output unavailable.')

    def _response(self, response):
        # Catch API 429 responses even when the document is HTTP 200. No URLs/bodies logged.
        if response.status == 429 and urlparse(response.url).hostname in ('www.instagram.com', 'instagram.com'):
            self.restriction = failure('rate_limit', marker='instagram_response_http_429')

    def _browser(self):
        if self.unavailable:
            raise self.unavailable
        if self.browser is None:
            self.browser = self.browser_factory().start()
            self.browser.context.route('**/*', lambda route: route.abort() if route.request.resource_type
                                       in ('image', 'media', 'font') else route.continue_())
            self.browser.page.on('response', self._response)
        return self.browser

    def _check(self, browser, status=None):
        error = self.restriction or page_failure(browser.page, status)
        if error is None and not has_session_cookie(browser.context):
            error = failure('login_required', marker='session_cookie_absent')
        if error:
            if error.stop_run:
                self.unavailable = error
            raise error

    def _navigate(self, browser, url):
        if self.restriction:
            raise self.restriction
        # Do not navigate away from an already visible challenge in a restored tab.
        if urlparse(browser.page.url).hostname in ('www.instagram.com', 'instagram.com'):
            visible = page_failure(browser.page)
            if visible and visible.stop_run:
                self.unavailable = visible
                raise visible
        time.sleep(InstagramPolicy.load().navigation_delay)
        self.navigation_status = None
        response = browser.page.goto(url, wait_until='domcontentloaded')
        self.navigation_status = response.status if response else None
        self._check(browser, response.status if response else None)
        try:
            browser.page.locator('main, article, [role="main"], [role="dialog"]').first.wait_for(state='attached', timeout=10000)
            browser.page.locator('a[href*="/direct/"], a[href*="/accounts/edit"], a[href*="/accounts/activity"], a[href*="/your_activity"]').first.wait_for(state='attached', timeout=10000)
        except PlaywrightTimeoutError:
            self._check(browser)
            raise failure('navigation_timeout', marker='expected_dom_wait_timeout') from None
        self._check(browser)
        if not session_authenticated(browser.context, browser.page):
            # Missing navigation with an existing cookie is not proof of expired login.
            raise failure('unexpected_structure', marker='authenticated_navigation_absent_with_cookie')

    def fetch_recent(self, username, limit, *, known_ids=frozenset()):
        if not re.fullmatch(r'[A-Za-z0-9_.]+', username) or not 1 <= limit <= MAX_LIMIT:
            raise InstagramCollectionError('Instagram source username or post limit is invalid.', reason='configuration')
        items = []
        try:
            self.stage = 'browser_launch'
            browser = self._browser()
            self.stage = 'profile_navigation'
            self._navigate(browser, canonical_profile_url(username))
            try:
                self.stage = 'profile_link_discovery'
                posts = discover_profile_posts(browser.page, username, limit)
                self._check(browser)
            finally:
                if self.diagnostic_sink:
                    try:
                        self._diagnostics(profile_diagnostics(browser.page, username))
                    except Exception:
                        self._diagnostics({'source_username': username, 'diagnostics': 'DOM diagnostics unavailable.'})
            if not posts:
                empty = browser.page.get_by_text(re.compile(r'^(?:no posts yet|ще немає дописів|дописів ще немає)[.!]?$', re.I))
                if empty.count() and empty.first.is_visible():
                    return []
                raise failure('unexpected_structure')
            cutoff = timezone.now() - timedelta(days=RECENT_DAYS)
            parsed_count = known_count = 0
            skipped = 0
            for url in posts:
                identity = post_identity(url)
                if identity[0] in known_ids:
                    known_count += 1
                    continue
                html, caption, published = '', '', None
                try:
                    self.stage = 'post_navigation'
                    self._navigate(browser, url)
                    self.stage = 'post_parsing'
                    current = post_identity(browser.page.url)
                    if not current or current[0] != identity[0]:
                        raise failure('unexpected_structure')
                    caption, timestamps = post_dom_candidates(browser.page)
                    published = post_timestamp(timestamps, current[1])
                    html = browser.page.content()
                    item = normalize_post_html(current[1], html, caption=caption, timestamp=published, username=username)
                    self._check(browser)
                    parsed_count += 1
                    self._diagnostics({'shortcode': item.external_id, 'post_kind': item.raw_metadata['post_kind'],
                                       'caption_available': bool(item.raw_text), 'published_at_available': item.published_at is not None,
                                       'private_image_url_available': bool(item.raw_image_url)})
                    if item.published_at is None or item.published_at >= cutoff:
                        items.append(item)
                except PlaywrightTimeoutError:
                    self._check(browser)
                    skipped += 1
                    self._diagnostics({**post_candidate_diagnostics(browser.page.url, html, caption=caption, timestamp=published),
                                       'reason': 'navigation_timeout', 'result': 'skipped_post'})
                    logger.warning('Instagram post skipped: reason=navigation_timeout; no retry.')
                except Exception as error:
                    if self.unavailable or self.restriction or (isinstance(error, InstagramCollectionError) and error.stop_run):
                        raise self.unavailable or self.restriction or error
                    reason = error.reason if isinstance(error, InstagramCollectionError) else 'unexpected_structure'
                    skipped += 1
                    self._diagnostics({**post_candidate_diagnostics(browser.page.url, html, caption=caption, timestamp=published),
                                       'reason': reason, 'result': 'skipped_post'})
                    logger.warning('Instagram post skipped: reason=%s; no retry.', reason)
            self._check(browser)
            self._diagnostics({'source_username': username, 'inspected_links': len(posts), 'known_links_skipped': known_count,
                               'normalized_items': parsed_count, 'recent_items': len(items), 'skipped_posts': skipped})
            if parsed_count == 0 and known_count == 0:
                logger.warning('Instagram result=no_parseable_posts; inspected=%s; skipped=%s; no security refusal detected.', len(posts), skipped)
                return InstagramPostItems(status='no_parseable_posts', inspected=len(posts), skipped=skipped)
            return InstagramPostItems(items, inspected=len(posts), skipped=skipped)
        except InstagramCollectionError as error:
            error.partial_items = tuple(items)
            self._annotate_failure(error)
            if error.stop_run:
                self.unavailable = error
            raise
        except PlaywrightTimeoutError:
            error = failure('navigation_timeout', marker='navigation_timeout')
            self._annotate_failure(error)
            raise error from None
        except Exception:
            error = failure('unexpected_structure')
            self._annotate_failure(error)
            raise error from None
        finally:
            # End Playwright's sync asyncio lifetime BEFORE any synchronous ORM writes.
            self.close()

    def _annotate_failure(self, error):
        try:
            error.diagnostics = failure_diagnostics(self.browser.page, error, stage=self.stage,
                                                    status=self.navigation_status) if self.browser else {
                'stage': self.stage, 'reason': error.reason, 'page_state_marker': getattr(error, 'marker', error.reason)}
        except Exception:
            error.diagnostics = {'stage': self.stage, 'reason': error.reason, 'page_state_marker': 'diagnostics_unavailable'}
        if self.restriction:
            error.diagnostics['observed_response_http_status'] = 429
        self._diagnostics(error.diagnostics)

    def close(self):
        if self.browser:
            self.browser.close()
            self.browser = None
