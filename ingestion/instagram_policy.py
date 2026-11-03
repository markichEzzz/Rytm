"""Conservative Instagram-only policy; environment/settings, no credential handling."""
from dataclasses import dataclass
import os
from pathlib import Path

from django.conf import settings
from .providers.instagram import InstagramCollectionError


def number(name, default, minimum, maximum):
    try:
        value = int(getattr(settings, name, os.environ.get(name, default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise InstagramCollectionError(f'{name} is outside its supported range.', reason='configuration') from None


@dataclass(frozen=True)
class InstagramPolicy:
    max_sources: int
    max_posts: int
    source_delay: int
    navigation_delay: int
    success_interval: int
    empty_interval: int
    failure_interval: int
    block_cooldown: int
    state_dir: Path

    @classmethod
    def load(cls):
        configured = getattr(settings, 'INSTAGRAM_COLLECTION_STATE_DIR', os.environ.get(
            'INSTAGRAM_COLLECTION_STATE_DIR', str(Path.home() / '.local/state/rytm-instagram')))
        path = Path(configured).expanduser()
        workspace = Path(settings.BASE_DIR).resolve().parent
        if not path.is_absolute():
            raise InstagramCollectionError('Instagram state directory must be an absolute dedicated path.', reason='configuration')
        path = path.resolve()
        inside_git = any((p / '.git').is_file() or (p / '.git/HEAD').is_file() for p in (path, *path.parents))
        if path == workspace or workspace in path.parents or path in (Path('/'), Path.home()) or inside_git:
            raise InstagramCollectionError('Instagram state directory must be dedicated and outside Git/workspace.', reason='configuration')
        return cls(
            number('INSTAGRAM_MAX_SOURCES_PER_RUN', 2, 1, 100),
            number('INSTAGRAM_MAX_POSTS_PER_SOURCE', 2, 1, 3),
            number('INSTAGRAM_SOURCE_DELAY_SECONDS', 60, 1, 86400),
            number('INSTAGRAM_NAVIGATION_DELAY_SECONDS', 10, 1, 3600),
            number('INSTAGRAM_CHECK_INTERVAL_SECONDS', 21600, 60, 604800),
            number('INSTAGRAM_NO_NEW_INTERVAL_SECONDS', 43200, 60, 604800),
            number('INSTAGRAM_FAILURE_COOLDOWN_SECONDS', 3600, 60, 604800),
            number('INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS', 86400, 3600, 2592000), path,
        )
