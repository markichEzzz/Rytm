"""Provider-neutral WSGI settings. No migrations or secrets in startup arguments."""
import os

port = int(os.getenv('PORT', '8000'))
if not 1 <= port <= 65535:
    raise ValueError('PORT must be between 1 and 65535.')
bind = f'0.0.0.0:{port}'
workers = int(os.getenv('WEB_CONCURRENCY', '1'))
if not 1 <= workers <= 8:
    raise ValueError('WEB_CONCURRENCY must be between 1 and 8.')
timeout = 30
graceful_timeout = 30
errorlog = '-'
loglevel = 'info'
capture_output = True
# Off by default; optional access logs exclude query, headers, referrer and IP.
accesslog = '-' if os.getenv('GUNICORN_ACCESS_LOG', 'false').lower() == 'true' else None
access_log_format = '%(m)s %(U)s %(s)s %(L)s'
