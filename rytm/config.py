"""Small environment parsers shared by settings and isolated configuration tests."""
import re
import ipaddress
from urllib.parse import urlsplit
import dj_database_url
from django.core.exceptions import ImproperlyConfigured


def boolean(value, name):
    text = str(value).strip().lower()
    if text not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ImproperlyConfigured(f'{name} must be true or false.')
    return text in {'true', '1', 'yes'}


def integer(value, name, minimum=0, maximum=86400):
    try:
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError
        return number
    except (TypeError, ValueError):
        raise ImproperlyConfigured(f'{name} is outside its supported integer range.') from None


def hosts(value, production=False):
    result = [part.strip() for part in value.split(',') if part.strip()]
    for host in result:
        if host.startswith('[') and host.endswith(']'):
            try:
                valid = ipaddress.ip_address(host[1:-1]).version == 6
            except ValueError:
                valid = False
        else:
            valid = len(host) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in host.split('.'))
        if host == '*' and not production:
            continue
        if not valid or '*' in host or '..' in host:
            raise ImproperlyConfigured('ALLOWED_HOSTS must contain hostnames/IPs without schemes, ports or wildcards.')
    if production and not result:
        raise ImproperlyConfigured('Production requires explicit ALLOWED_HOSTS.')
    return list(dict.fromkeys(result))


def origins(value, production=False):
    result = [part.strip() for part in value.split(',') if part.strip()]
    for origin in result:
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme in ({'https'} if production else {'http', 'https'}) and
                     parsed.hostname and parsed.port != 0 and parsed.username is None and parsed.password is None and
                     not parsed.path and not parsed.query and not parsed.fragment and '*' not in origin and
                     not any(c.isspace() for c in origin))
        except ValueError:
            valid = False
        if not valid:
            raise ImproperlyConfigured('CSRF_TRUSTED_ORIGINS must contain complete trusted origins (HTTPS in production), without paths or credentials.')
    return list(dict.fromkeys(result))


def database_url(value, *, production, ssl_required=None, conn_max_age=None):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'postgres', 'postgresql', 'postgis'}:
            raise ValueError
        if not parsed.hostname or not parsed.path.strip('/') or not parsed.username:
            raise ValueError
        config = dj_database_url.parse(value)
    except (ValueError, TypeError, KeyError):
        # Database URLs contain passwords: never include the rejected value.
        raise ImproperlyConfigured('DATABASE_URL must be a valid PostgreSQL/PostGIS URL with host, database and user.') from None
    config['ENGINE'] = 'django.contrib.gis.db.backends.postgis'
    config['CONN_MAX_AGE'] = integer(conn_max_age if conn_max_age is not None else (60 if production else 0), 'DATABASE_CONN_MAX_AGE', maximum=600)
    config['CONN_HEALTH_CHECKS'] = production
    options = config.setdefault('OPTIONS', {})
    require_ssl = production if ssl_required is None else ssl_required
    mode = options.get('sslmode')
    if mode and mode not in {'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'}:
        raise ImproperlyConfigured('DATABASE_URL sslmode is invalid.')
    if require_ssl:
        if mode and mode not in {'require', 'verify-ca', 'verify-full'}:
            raise ImproperlyConfigured('DATABASE_SSL_REQUIRE=True conflicts with an insecure URL sslmode.')
        options.setdefault('sslmode', 'require')  # Never downgrade verify-full.
    return config
