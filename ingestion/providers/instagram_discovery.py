"""Bounded semantic link discovery and opt-in, non-secret browser diagnostics."""
import re
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .instagram import InstagramCollectionError
from .instagram_browser import SESSION_REQUIRED, blocked_page
from .instagram_html import recent_post_links, post_identity

LINK_SELECTOR = 'a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]'
HREFS_JS = '(elements) => elements.map(e => e.getAttribute("href"))'
MAX_SCROLLS = 2


def dismiss_harmless_overlays(page):
    if blocked_page(page):
        raise InstagramCollectionError(SESSION_REQUIRED)
    # Dismiss only an explicitly identified harmless dialog. Never touch security/login UI.
    patterns = (
        (r'cookies|cookie|файл[иів]* cookie', r'decline optional cookies|only allow essential cookies|відхилити необов.?язкові'),
        (r'turn on notifications|увімкн[уі]ть сповіщення', r'not now|не зараз'),
    )
    for dialog_pattern, button_pattern in patterns:
        dialogs = page.get_by_role('dialog').filter(has_text=re.compile(dialog_pattern, re.I))
        if dialogs.count() != 1 or not dialogs.first.is_visible():
            continue
        security = dialogs.first.get_by_text(re.compile(r'captcha|checkpoint|verify.*identity|security verification|перевірка.*особи', re.I))
        if security.count():
            continue
        button = dialogs.first.get_by_role('button', name=re.compile(rf'^(?:{button_pattern})$', re.I))
        if button.count() == 1 and button.is_visible():
            button.click(timeout=2000)


def discover_profile_posts(page, username, limit):
    dismiss_harmless_overlays(page)
    try:
        page.wait_for_load_state('networkidle', timeout=2000)
    except PlaywrightTimeoutError:
        pass  # Instagram may keep background connections open.
    for attempt in range(MAX_SCROLLS + 1):
        if blocked_page(page):
            raise InstagramCollectionError(SESSION_REQUIRED)
        try:
            page.locator(LINK_SELECTOR).first.wait_for(state='attached', timeout=4000)
        except PlaywrightTimeoutError:
            pass
        # Prefer actual main content; support role-based or class-free profile layouts as fallback.
        scoped = page.locator('main a[href], article a[href], [role="main"] a[href]').evaluate_all(HREFS_JS)
        posts = recent_post_links(scoped, limit, username=username)
        if not posts:
            posts = recent_post_links(page.locator('a[href]').evaluate_all(HREFS_JS), limit, username=username)
        if posts:
            return posts
        if attempt < MAX_SCROLLS:
            page.mouse.wheel(0, 650)
            page.wait_for_timeout(400)
    return []


def safe_page_url(url):
    parsed = urlparse(url)
    if parsed.hostname not in ('www.instagram.com', 'instagram.com'):
        return '[unexpected host]'
    identity = post_identity(url)
    if identity:
        return identity[1]
    if re.fullmatch(r'/[A-Za-z0-9_.]+/', parsed.path) or parsed.path == '/':
        return f'https://www.instagram.com{parsed.path}'
    for prefix in ('/accounts/login', '/challenge', '/checkpoint'):
        if parsed.path.startswith(prefix):
            return f'https://www.instagram.com{prefix}/[details redacted]'
    return 'https://www.instagram.com/[unrecognized path]'


def profile_diagnostics(page, username):
    hrefs = page.locator('a[href]').evaluate_all(HREFS_JS)
    counts = {kind: sum(isinstance(href, str) and f'/{kind}/' in urlparse(href).path for href in hrefs)
              for kind in ('p', 'reel', 'tv')}
    title = re.sub(r'[\x00-\x1f]', ' ', page.title())[:160]
    header = page.locator('header,h1,h2,[role="heading"]').evaluate_all(
        '(elements, username) => elements.some(e => e.innerText.includes(username) && e.getBoundingClientRect().height > 0)',
        username,
    )
    return {
        'profile_url': f'https://www.instagram.com/{username}/', 'final_url': safe_page_url(page.url),
        'page_title': title, 'login_or_checkpoint_shown': blocked_page(page),
        'profile_header_visible': bool(header), 'total_anchors': len(hrefs),
        'post_links': counts['p'], 'reel_links': counts['reel'], 'tv_links': counts['tv'],
    }


def empty_profile_error(page, username):
    # Only call a profile private/unavailable when its own UI says so.
    private = page.get_by_text(re.compile(r'^(?:this account is private|цей (?:обліковий запис|акаунт) приватний)[.!]?$', re.I))
    if private.count() and private.first.is_visible():
        return 'Instagram profile is explicitly marked private; collection cannot continue.'
    unavailable = page.get_by_text(re.compile(r"sorry, this page isn.t available|сторінка недоступна", re.I))
    if unavailable.count() and unavailable.first.is_visible():
        return 'Instagram profile is unavailable according to the page.'
    return ('Instagram profile loaded but no post/reel links were found after bounded waits and two small scrolls. '
            'Use --debug-browser for sanitized DOM diagnostics.')
