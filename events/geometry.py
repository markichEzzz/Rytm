"""Parse only WGS84 points, without handing untrusted GeoJSON CRS to GDAL."""
import json
import math
import re
from django.contrib.gis.geos import Point
from rest_framework import serializers


class WGS84PointField(serializers.Field):
    default_error_messages = {'invalid': 'Location must be a two-dimensional WGS84 point.'}

    def to_representation(self, value):
        return value.ewkt

    def to_internal_value(self, value):
        try:
            if isinstance(value, Point):
                if value.empty or value.hasz or value.srid not in (None, 4326):
                    self.fail('invalid')
                x, y = value.x, value.y
            else:
                if isinstance(value, str):
                    if len(value) > 1024:
                        self.fail('invalid')
                    if value.lstrip().startswith('{'):
                        value = json.loads(value)
                    else:
                        number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
                        match = re.fullmatch(r'\s*(?:SRID=4326;)?POINT\s*\(\s*(' + number + r')\s+(' + number + r')\s*\)\s*', value, re.I)
                        if not match:
                            self.fail('invalid')
                        value = {'type': 'Point', 'coordinates': [float(match[1]), float(match[2])]}
                if not isinstance(value, dict) or value.get('type') != 'Point':
                    self.fail('invalid')
                # Omitted CRS means WGS84; never interpret a URL/file CRS.
                crs = value.get('crs')
                if crs is not None and crs != {'type': 'name', 'properties': {'name': 'EPSG:4326'}}:
                    self.fail('invalid')
                coordinates = value['coordinates']
                if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
                    self.fail('invalid')
                if any(isinstance(c, bool) or not isinstance(c, (int, float)) for c in coordinates):
                    self.fail('invalid')
                x, y = coordinates
            if not math.isfinite(x) or not math.isfinite(y) or not -180 <= x <= 180 or not -90 <= y <= 90:
                self.fail('invalid')
            return Point(x, y, srid=4326)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.fail('invalid')
