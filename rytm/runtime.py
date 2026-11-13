"""Native library requirements for spatial-reference security protections."""
from django.core.exceptions import ImproperlyConfigured


def require_safe_gdal():
    # GeoDjango calls the system C library directly; a Python GDAL wheel is not
    # the library selector and must not be used to infer the loaded version.
    from django.contrib.gis.gdal import GDAL_VERSION

    if GDAL_VERSION < (3, 9, 0):
        version = '.'.join(map(str, GDAL_VERSION))
        raise ImproperlyConfigured(
            f'Production requires GDAL >= 3.9.0 for spatial-reference protection; loaded {version}. '
            'Use the validated container runtime or upgrade the native GIS libraries.'
        )
    return GDAL_VERSION
