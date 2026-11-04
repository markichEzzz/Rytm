"""Classify Instagram's visible refusal states; never interact with security UI."""
import re
from urllib.parse import urlparse
import instaloader
from .instagram import InstagramCollectionError

MESSAGES = {
    'rate_limit': 'Instagram rate limited access (429 or explicit rate-limit UI); no retry was made.',
    'challenge': 'Instagram challenge/checkpoint requires manual verification; collection stopped without retry.',
    'checkpoint': 'Instagram checkpoint requires manual verification; collection stopped without retry.',
    'verification': 'Instagram login/identity verification requested; collection stopped without retry.',
    'temporary_block': 'Instagram reports a temporary block or suspicious activity; collection stopped without retry.',
    'login_required': 'Instagram browser session is not authenticated. Run: python manage.py instagram_browser_login',
    'access_blocked': 'Instagram refused access (400/403); collection stopped without retry.',
    'unexpected_redirect': 'Instagram redirected outside the expected host; collection stopped without retry.',
    'not_found': 'Instagram source/page not found (404); no retry was made.',
    'private_account': 'Instagram profile is explicitly marked private; no retry was made.',
    'unavailable': 'Instagram page is explicitly unavailable; no retry was made.',
    'timeout': 'Instagram page/navigation timed out; no retry was made.',
    'unexpected_structure': 'Instagram page has no usable expected structure; no retry was made.',
    'profile_unavailable': 'Instagram source profile is unavailable; no retry was made.',
    'post_unavailable': 'Instagram post is unavailable; no retry was made.',
    'private_profile': 'Instagram source profile is explicitly private; no retry was made.',
    'navigation_timeout': 'Instagram navigation/expected page structure timed out; no retry was made.',
}
STOP_REASONS = frozenset(('rate_limit', 'challenge', 'checkpoint', 'verification', 'temporary_block', 'login_required', 'access_blocked', 'unexpected_redirect'))


def failure(reason, *, marker=None):
    error = InstagramCollectionError(MESSAGES[reason], reason=reason, stop_run=reason in STOP_REASONS)
    error.marker = marker or reason
    return error


def page_failure(page, status=None):
    path = urlparse(page.url).path.lower()
    host = urlparse(page.url).hostname
    from .instagram_html import post_identity
    unavailable = 'post_unavailable' if post_identity(page.url) else 'profile_unavailable'
    if status == 429:
        return failure('rate_limit', marker='navigation_http_429')
    for route in ('challenge', 'checkpoint'):
        if path == '/' + route or path.startswith('/' + route + '/'):
            return failure(route, marker='url_' + route)
    if any(path.startswith(part) for part in ('/accounts/confirm', '/accounts/verify', '/accounts/two_factor', '/accounts/suspended')):
        return failure('verification', marker='url_verification')
    if path.startswith('/accounts/login') or status == 401:
        return failure('login_required', marker='navigation_http_401' if status == 401 else 'url_login')
    if status in (400, 403):
        return failure('access_blocked')
    if status == 404:
        return failure(unavailable, marker='navigation_http_404')
    if status is not None and status >= 500:
        return failure(unavailable, marker='navigation_http_5xx')
    if host not in ('www.instagram.com', 'instagram.com'):
        return failure('unexpected_redirect')
    # Exact security headings/banners, not unbounded body scraping or exception output.
    patterns = (
        ('rate_limit', r'^(?:too many requests|please wait a few minutes before you try again)[.!]?$'),
        ('challenge', r'^(?:challenge required|security check|captcha|перевірка безпеки)[.!]?$'),
        ('checkpoint', r'^checkpoint required[.!]?$'),
        ('verification', r'^(?:confirm it.?s you|confirm your identity|verify your identity|verify your account|enter (?:the )?security code|help us confirm you own this account|підтвердьте свою особу)[.!]?$'),
        ('temporary_block', r'^(?:try again later|we restrict certain activity|your account has been temporarily blocked|we detected suspicious activity|suspicious login attempt|we suspect automated behavior|спробуйте ще раз пізніше|ми обмежуємо певні дії)(?:[.!\s].*)?$'),
        ('private_profile', r'^(?:this account is private|цей (?:обліковий запис|акаунт) приватний)[.!]?$'),
        (unavailable, r"^(?:sorry, this page isn.t available|сторінка недоступна)[.!]?$"),
    )
    for reason, pattern in patterns:
        locator = page.get_by_text(re.compile(pattern, re.I))
        if locator.count() and locator.first.is_visible():
            return failure(reason, marker='visible_ui_' + reason)
    inputs = page.locator('input[name="username"], input[name="password"]')
    if inputs.count() and inputs.first.is_visible():
        return failure('login_required', marker='visible_login_form')
    return None


def instaloader_failure(error):
    # Inspect locally only to select a FIXED code/message. Never forward the text.
    text = str(error).casefold()
    if isinstance(error, instaloader.ProfileNotExistsException) or re.search(r'\b404\b', text):
        return failure('not_found')
    if re.search(r'\b429\b|too many requests', text):
        return failure('rate_limit')
    if 'checkpoint' in text or 'challenge' in text:
        return failure('challenge')
    if 'two-factor' in text or 'verification' in text:
        return failure('verification')
    if 'suspicious' in text or 'temporarily blocked' in text or 'please wait' in text:
        return failure('temporary_block')
    if isinstance(error, instaloader.LoginRequiredException) or re.search(r'\b401\b', text):
        return failure('login_required')
    if isinstance(error, instaloader.AbortDownloadException) or re.search(r'\b(?:400|403)\b', text):
        return failure('access_blocked')
    if 'timeout' in type(error).__name__.casefold():
        return failure('timeout')
    return failure('unexpected_structure')
