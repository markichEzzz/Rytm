"""Canonical configured Instagram profile identities, without sharing parameters."""
import re
from urllib.parse import urlparse


def canonical_profile_url(value):
    value = value.strip()
    if '://' in value or value.startswith('//'):
        parsed = urlparse('https:' + value if value.startswith('//') else value)
        if (parsed.scheme not in ('https', 'http') or parsed.hostname not in ('instagram.com', 'www.instagram.com')
                or parsed.username or parsed.password or parsed.port not in (None, 443, 80)):
            raise ValueError('Expected an Instagram profile URL.')
        username = parsed.path.strip('/')
    else:
        username = value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', username) or username.lower() in {'p', 'reel', 'reels', 'explore', 'direct', 'accounts'}:
        raise ValueError('Expected one Instagram username.')
    return f'https://www.instagram.com/{username.lower()}/'
