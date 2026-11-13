from unittest.mock import patch
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from .runtime import require_safe_gdal


class NativeRuntimeTests(SimpleTestCase):
    @override_settings(IS_PRODUCTION=True)
    def test_production_startup_rejects_old_loaded_gdal(self):
        for version in ((3, 6, 2), (3, 8, 4)):
            with self.subTest(version=version), patch('django.contrib.gis.gdal.GDAL_VERSION', version):
                with self.assertRaisesMessage(ImproperlyConfigured, 'Production requires GDAL >= 3.9.0'):
                    apps.get_app_config('rytm').ready()

    @override_settings(IS_PRODUCTION=True)
    def test_production_startup_accepts_supported_loaded_gdal(self):
        for version in ((3, 9, 0), (3, 10, 3)):
            with self.subTest(version=version), patch('django.contrib.gis.gdal.GDAL_VERSION', version):
                apps.get_app_config('rytm').ready()
                self.assertEqual(require_safe_gdal(), version)

    @override_settings(IS_PRODUCTION=False)
    def test_development_keeps_django_warning_instead_of_blocking_local_tests(self):
        with patch('django.contrib.gis.gdal.GDAL_VERSION', (3, 8, 4)):
            apps.get_app_config('rytm').ready()
            # Direct validation still rejects it; no warning has been silenced.
            with self.assertRaises(ImproperlyConfigured):
                require_safe_gdal()
