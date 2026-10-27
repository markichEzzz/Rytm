#!/usr/bin/env python3
"""Read-only HTTPS smoke checks. Never authenticates, follows redirects or writes data."""
import argparse
import json
import os
from urllib.parse import urlsplit
import requests

CHECKS = ('/api/health/', '/api/events/', '/api/routes/', '/privacy/', '/terms/', '/account-deletion/')
MAX_BYTES = 5 * 1024 * 1024


def origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or
                parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise ValueError('BASE_URL must be an HTTPS origin without credentials, query or path.') from None
    return value.rstrip('/')


def check(base_url, *, get=None):
    base = origin(base_url)
    if get is None:
        # Do not inherit ~/.netrc credentials, authenticated proxies or cookies.
        with requests.Session() as session:
            session.trust_env = False
            session.auth = None
            def public_get(url, **kwargs):
                session.cookies.clear()
                return session.get(url, **kwargs)
            return check(base, get=public_get)
    results = []
    for path in CHECKS:
        try:
            with get(base + path, timeout=(5, 15), allow_redirects=False, stream=True, verify=True,
                     headers={'Accept': 'application/json' if path.startswith('/api/') else 'text/html'}) as response:
                if response.status_code != 200:
                    results.append((path, False, f'HTTP {response.status_code} (redirects are not followed)'))
                    continue
                chunks = []; size = 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('response too large')
                    chunks.append(chunk)
                body = b''.join(chunks)
                if path.startswith('/api/'):
                    data = json.loads(body)
                    valid = data == {'status': 'ok'} if path == '/api/health/' else (
                        isinstance(data, list) or isinstance(data, dict) and isinstance(data.get('results'), list))
                else:
                    valid = ('text/html' in response.headers.get('Content-Type', '') and b'<html' in body.lower()
                             and b'Rytm' in body and b'[CONTACT_EMAIL]' not in body
                             and b'[LEGAL_ENTITY_OR_DEVELOPER_NAME]' not in body)
                results.append((path, bool(valid), 'ok' if valid else 'unexpected content or unresolved legal placeholders'))
        except (requests.RequestException, ValueError, UnicodeError):
            # Do not echo exception URLs, response bodies, credentials or internals.
            results.append((path, False, 'network/TLS/response validation failed'))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.getenv('BASE_URL', ''))
    args = parser.parse_args()
    try:
        results = check(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    for path, passed, reason in results:
        print(f'{"PASS" if passed else "FAIL"} {path}: {reason}')
    return 0 if all(passed for _, passed, _ in results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
