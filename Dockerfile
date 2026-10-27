FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production DJANGO_DEBUG=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal32 libgeos-c1v5 libproj25 libpq5 \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rytm && useradd --uid 10001 --gid rytm --no-create-home rytm
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Allowlist source: no env, private browser profile, service key, dump or venv.
COPY manage.py start.sh gunicorn.conf.py ./
COPY rytm/ ./rytm/
COPY accounts/ ./accounts/
COPY events/ ./events/
COPY ingestion/ ./ingestion/
COPY tour_routes/ ./tour_routes/
COPY docs/legal/ ./docs/legal/
COPY scripts/production_smoke_test.py ./scripts/production_smoke_test.py
RUN mkdir -p staticfiles && chown rytm:rytm staticfiles && chmod 0555 start.sh
USER rytm
EXPOSE 8000
# PORT is supplied by the platform. No migrate, cloud creation or deployment.
CMD ["./start.sh"]
