#!/bin/sh
set -eu
# A production launcher must not silently select developer defaults.
export ENVIRONMENT="${ENVIRONMENT:-production}"
# Static assets only. Migrations are an explicit operator-controlled release step.
python manage.py collectstatic --noinput
exec gunicorn --config gunicorn.conf.py rytm.wsgi:application
