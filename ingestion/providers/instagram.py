"""Bounded Instagram metadata access. Never download media or persist credentials."""
from datetime import timedelta, timezone as datetime_timezone
from itertools import islice
import os
import re

import instaloader
from django.utils import timezone

from ingestion.collectors import CollectedSourceItem
from ingestion.models import SourceAccount

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
RECENT_DAYS = 14


class InstagramCollectionError(Exception):
    """Safe user-facing failure; provider responses/cookies must never be logged."""


class InstaloaderProvider:
    def __init__(self):
        self.loader = None
        self.unavailable = None

    def _client(self):
        if self.unavailable:
            raise InstagramCollectionError(self.unavailable)
        if self.loader:
            return self.loader
        loader = instaloader.Instaloader(
            quiet=True, sleep=True, max_connection_attempts=1, request_timeout=20,
            download_pictures=False, download_videos=False, download_video_thumbnails=False,
            download_geotags=False, download_comments=False, save_metadata=False,
            iphone_support=False, fatal_status_codes=[400, 401, 403, 404, 429],
        )
        # Quiet mode does not silence library errors, which can include HTTP bodies.
        loader.context.error = lambda *args, **kwargs: None
        username = os.environ.get('INSTAGRAM_USERNAME', '').strip()
        password = os.environ.get('INSTAGRAM_PASSWORD', '')
        session_file = os.environ.get('INSTAGRAM_SESSION_FILE', '').strip()
        try:
            if (password or session_file) and not username:
                raise InstagramCollectionError('INSTAGRAM_USERNAME is required for authentication.')
            if session_file:
                # Explicit trusted local session only; no implicit home-directory lookup.
                loader.load_session_from_file(username, session_file)
            elif username and password:
                loader.login(username, password)
            elif username:
                raise InstagramCollectionError('Set INSTAGRAM_PASSWORD or INSTAGRAM_SESSION_FILE with INSTAGRAM_USERNAME.')
        except Exception as error:
            loader.close()
            self.unavailable = (str(error) if isinstance(error, InstagramCollectionError) else
                                'Instagram authentication/session failed; configure a valid local session or credentials. No retry was made.')
            raise InstagramCollectionError(self.unavailable) from None
        self.loader = loader
        return loader

    def fetch_recent(self, username, limit):
        loader = self._client()
        cutoff = timezone.now() - timedelta(days=RECENT_DAYS)
        try:
            profile = instaloader.Profile.from_username(loader.context, username)
            if profile.is_private:
                raise InstagramCollectionError('Private accounts are outside the public collection scope.')
            items = []
            # Limit scanned posts as well as returned posts; pinned old posts never trigger history crawling.
            for post in islice(profile.get_posts(), limit):
                published = post.date_utc
                if timezone.is_naive(published):
                    published = published.replace(tzinfo=datetime_timezone.utc)
                if published < cutoff:
                    continue
                items.append(CollectedSourceItem(
                    external_id=post.shortcode,
                    source_url=f'https://www.instagram.com/p/{post.shortcode}/',
                    raw_text=post.caption or '', published_at=published,
                    raw_image_url=post.url or '',
                    raw_metadata={
                        'provider': 'instaloader', 'shortcode': post.shortcode,
                        'media_id': str(post.mediaid), 'source_username': username,
                        'media_type': post.typename, 'is_video': post.is_video,
                    },
                ))
            return items
        except InstagramCollectionError:
            raise
        except Exception as error:
            # Do not disclose provider exception text, HTTP bodies, session paths or secrets.
            if isinstance(error, instaloader.AbortDownloadException):
                if '"404 ' in str(error):
                    message = 'Instagram account/resource not found; no retry was made.'
                else:
                    message = 'Instagram blocked access or requested a challenge/rate limit; no retry was made.'
                    self.unavailable = message  # Avoid repeated requests after a run-wide block.
            elif isinstance(error, instaloader.ProfileNotExistsException):
                message = 'Instagram account not found; no retry was made.'
            elif isinstance(error, instaloader.LoginRequiredException):
                message = 'Instagram requires authentication; configure a valid local session or credentials.'
                self.unavailable = message
            else:
                message = f'Instagram metadata request failed ({type(error).__name__}); no retry was made.'
            raise InstagramCollectionError(message) from None

    def close(self):
        if self.loader:
            self.loader.close()
            self.loader = None


def selected_provider():
    name = os.environ.get('INSTAGRAM_COLLECTION_PROVIDER', 'instaloader').strip().lower()
    if name == 'instaloader':
        return InstaloaderProvider()
    if name == 'playwright':
        from .instagram_playwright import PlaywrightInstagramProvider
        return PlaywrightInstagramProvider()
    raise InstagramCollectionError('INSTAGRAM_COLLECTION_PROVIDER must be instaloader or playwright.')


class InstagramCollector:
    """The SourceCollector interface, restricted to enabled configured database accounts."""
    def __init__(self, limit=DEFAULT_LIMIT, provider=None):
        if not 1 <= limit <= MAX_LIMIT:
            raise InstagramCollectionError(f'Limit must be between 1 and {MAX_LIMIT}.')
        self.limit = limit
        self.provider = provider if provider is not None else selected_provider()

    def fetch_items(self, source_account):
        source = SourceAccount.objects.filter(
            pk=source_account.pk, platform=SourceAccount.Platform.INSTAGRAM, enabled=True,
        ).first()
        if source is None or not re.fullmatch(r'[A-Za-z0-9_.]+', source.username):
            raise InstagramCollectionError('Only enabled configured Instagram accounts may be collected.')
        return self.provider.fetch_recent(source.username, self.limit)

    def close(self):
        self.provider.close()
