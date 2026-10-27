"""Explicitly authorized source-image copies, made only during moderator approval."""
from io import BytesIO
from uuid import uuid4

from PIL import Image
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from .image_text import download_image, MAX_BYTES, MAX_PIXELS


def source_image_url(candidate):
    return candidate.source_image_url or (candidate.raw_source_item.raw_image_url if candidate.raw_source_item_id else '')


def publication_image(candidate):
    try:
        data = download_image(source_image_url(candidate))
        with Image.open(BytesIO(data)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > MAX_PIXELS:
                raise ValueError('Unsupported image')
            image.load()  # Reject truncated/invalid content, not merely a plausible MIME header.
            output = BytesIO()
            if image.mode in ('RGBA', 'LA') or image.info.get('transparency') is not None:
                rgba = image.convert('RGBA')
                clean = Image.new('RGB', image.size, 'white')
                clean.paste(rgba, mask=rgba.getchannel('A'))
            else:
                clean = image.convert('RGB')
            clean.save(output, format='JPEG', quality=88)  # Discard source metadata/EXIF.
        if output.tell() > MAX_BYTES:
            raise ValueError('Converted image too large')
        return f'source-{uuid4().hex}.jpg', ContentFile(output.getvalue())
    except Exception:
        raise ValidationError('Source image is unavailable or invalid. Upload a public photo manually and try again.') from None
