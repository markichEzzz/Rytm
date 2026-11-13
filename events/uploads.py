"""Image validation before any Cloudinary storage call."""
from pathlib import Path
from rest_framework import serializers


class EventPhotoField(serializers.ImageField):
    def to_internal_value(self, data):
        if getattr(data, 'size', 0) > 10 * 1024 * 1024:
            raise serializers.ValidationError('Photo must be at most 10 MiB.')
        if (getattr(data, 'content_type', None) not in ('image/jpeg', 'image/png')
                or Path(getattr(data, 'name', '')).suffix.lower() not in ('.jpg', '.jpeg', '.png')):
            raise serializers.ValidationError('Choose a JPEG or PNG image.')
        value = super().to_internal_value(data)
        if value.image.format not in ('JPEG', 'PNG'):
            raise serializers.ValidationError('Choose a valid JPEG or PNG image.')
        width, height = value.image.size
        if width * height > 16_000_000 or max(width, height) > 8192:
            raise serializers.ValidationError('Photo must be at most 16 megapixels and 8192 pixels per side.')
        return value
