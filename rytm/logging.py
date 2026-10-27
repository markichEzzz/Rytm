"""Preserve operational tracebacks while redacting common credential forms."""
import logging
import os
import re


def redact(text):
    text = re.sub(r'(?i)Bearer\s+[^\s"\',;]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'''(?i)((?:["']?)(?:password|refresh|access|id_token|api_key|secret_key)(?:["']?)\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED JWT]', text)
    text = re.sub(r'(?i)(postgres(?:ql)?://)[^@\s]+@', r'\1[REDACTED]@', text)
    for key in ('DJANGO_SECRET_KEY', 'SECRET_KEY', 'OPENAI_API_KEY', 'CLOUDINARY_API_KEY', 'CLOUDINARY_API_SECRET', 'POSTGRES_PASSWORD', 'DATABASE_URL'):
        value = os.getenv(key)
        if value and len(value) >= 8:
            text = text.replace(value, '[REDACTED]')
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))
