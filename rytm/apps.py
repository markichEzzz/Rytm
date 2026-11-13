"""Fail closed on unsafe production native libraries for every Django entrypoint."""
from django.apps import AppConfig
from django.conf import settings


class RytmConfig(AppConfig):
    name = 'rytm'

    def ready(self):
        if settings.IS_PRODUCTION:
            from .runtime import require_safe_gdal
            require_safe_gdal()
