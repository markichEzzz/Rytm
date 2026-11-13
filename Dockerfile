FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production DJANGO_DEBUG=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal36=3.10.3+dfsg-1 libgeos-c1t64 libproj25 libpq5 \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rytm && useradd --uid 10001 --gid rytm --create-home --home-dir /home/rytm rytm
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
# Fail the build if GeoDjango loads an unsupported native library. This check
# needs neither Django settings nor database/cloud credentials.
RUN python -c "from rytm.runtime import require_safe_gdal; print('GDAL runtime:', require_safe_gdal())"
RUN mkdir -p staticfiles && chown -R rytm:rytm /app /home/rytm && chmod 0555 start.sh
ENV HOME=/home/rytm
USER rytm
EXPOSE 8000
# PORT is supplied by the platform. No migrate, cloud creation or deployment.
CMD ["./start.sh"]
