"""Dedicated persistent Chromium profile. Login/verification always belong to the user."""
import os
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from playwright.sync_api import sync_playwright

from .instagram import InstagramCollectionError

SESSION_REQUIRED = ('Instagram browser session is not authenticated. Run: '
                    'python manage.py instagram_browser_login')


def profile_directory():
    configured = os.environ.get('INSTAGRAM_PLAYWRIGHT_PROFILE_DIR', '').strip()
    path = Path(configured).expanduser() if configured else Path.home() / '.local/share/rytm-instagram-profile'
    if not path.is_absolute():
        raise InstagramCollectionError('Instagram browser profile must use an absolute dedicated path outside the workspace.')
    path = path.resolve()
    workspace = Path(settings.BASE_DIR).resolve().parent
    if path == workspace or workspace in path.parents:
        raise InstagramCollectionError('Instagram browser profile must be outside the Rytm workspace.')
    if any((parent / '.git').exists() for parent in (path, *path.parents)):
        raise InstagramCollectionError('Instagram browser profile must be outside Git repositories.')
    if path == Path.home() or path == Path('/'):
        raise InstagramCollectionError('Choose a dedicated Instagram browser profile directory.')
    return path


def collection_headless():
    value = os.environ.get('INSTAGRAM_PLAYWRIGHT_HEADLESS', 'false').strip().lower()
    if value not in ('true', 'false'):
        raise InstagramCollectionError('INSTAGRAM_PLAYWRIGHT_HEADLESS must be true or false.')
    return value == 'true'


def blocked_page(page):
    parsed = urlparse(page.url)
    return (parsed.hostname not in ('www.instagram.com', 'instagram.com')
            or any(part in parsed.path for part in ('/accounts/login', '/challenge', '/checkpoint'))
            or page.locator('input[name="username"], input[name="password"]').count() > 0)


def has_session_cookie(context):
    return any(cookie.get('name') == 'sessionid' and cookie.get('value')
               for cookie in context.cookies(['https://www.instagram.com/']))


def session_authenticated(context, page):
    if blocked_page(page):
        return False
    # Only inspect a boolean presence. Cookie values never leave this helper or enter logs.
    if not has_session_cookie(context):
        return False
    links = page.locator('a[href]').evaluate_all('(elements) => elements.map(e => e.getAttribute("href"))')
    authenticated_nav = any(isinstance(link, str) and urlparse(link).path.startswith(
        ('/direct/', '/accounts/edit', '/accounts/activity', '/your_activity')
    ) for link in links)
    usable_home_feed = urlparse(page.url).path == '/' and page.locator('main article').count() > 0
    return authenticated_nav or usable_home_feed


class InstagramBrowser:
    def __init__(self, *, headed_login=False):
        self.headless = False if headed_login else collection_headless()
        self.path = profile_directory()
        self.runtime = self.context = self.page = None

    def start(self):
        try:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.runtime = sync_playwright().start()
            self.context = self.runtime.chromium.launch_persistent_context(
                user_data_dir=str(self.path), headless=self.headless, accept_downloads=False,
                timeout=30000,
            )
            self.context.set_default_timeout(10000)
            self.context.set_default_navigation_timeout(30000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            return self
        except Exception:
            self.close()
            raise InstagramCollectionError(
                'Could not launch the persistent Instagram Chromium profile. Install Chromium with '
                'python -m playwright install chromium; close any browser using this profile and check display/path permissions.'
            ) from None

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass  # No provider exception bodies/session details in terminal output.
        finally:
            self.context = self.page = None
            if self.runtime:
                try:
                    self.runtime.stop()
                except Exception:
                    pass
                self.runtime = None
