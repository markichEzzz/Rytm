"""Small Instagram HTML helpers. No full page HTML or session data is persisted."""
from datetime import datetime
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlparse

from django.utils import timezone

from ingestion.collectors import CollectedSourceItem
from .instagram import InstagramCollectionError

POST_PATH = re.compile(r'^/(?:([A-Za-z0-9_.]+)/)?(p|reel|tv)/([A-Za-z0-9_-]+)/?$')


def post_identity(url):
    try:
        parsed = urlparse(urljoin('https://www.instagram.com/', url))
        port = parsed.port
    except (ValueError, TypeError):
        return None
    if (parsed.scheme != 'https' or parsed.hostname not in ('www.instagram.com', 'instagram.com')
            or parsed.username or parsed.password or port not in (None, 443)):
        return None
    match = POST_PATH.fullmatch(parsed.path)
    if not match:
        return None
    return match[3], f'https://www.instagram.com/{match[2]}/{match[3]}/'


def recent_post_links(links, limit, username=None):
    result, seen = [], set()
    for href in links:
        if not isinstance(href, str):
            continue
        try:
            parsed = urlparse(urljoin('https://www.instagram.com/', href))
        except ValueError:
            continue
        match = POST_PATH.fullmatch(parsed.path)
        if username and match and match[1] and match[1].casefold() != username.casefold():
            continue
        identity = post_identity(href)
        if identity and identity[0] not in seen:
            seen.add(identity[0])
            result.append(identity[1])
            if len(result) >= limit:
                break
    return result


def post_timestamp(records, url):
    identity = post_identity(url)
    for record in records:
        link = post_identity(record.get('href') or '')
        if link and link[0] == identity[0]:
            return record.get('datetime')
    # Do not assign a random comment time to the original post.
    unique = {record.get('datetime') for record in records if record.get('datetime')}
    return next(iter(unique)) if len(unique) == 1 else None


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.structured = []
        self.json_parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name')
            if key and attrs.get('content'):
                self.meta[key] = attrs['content']
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.json_parts = []

    def handle_data(self, data):
        if self.json_parts is not None:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.json_parts is not None:
            try:
                self.structured.append(json.loads(''.join(self.json_parts)))
            except (ValueError, TypeError):
                pass  # Semantic DOM or Open Graph metadata may still be usable.
            self.json_parts = None


def structured_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from structured_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ('@graph', 'mainEntity', 'associatedMedia', 'image', 'video'):
            if key in value:
                yield from structured_nodes(value[key])


def optional_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else None


def normalize_post_html(url, html, *, caption='', timestamp=None, image_url='', username=''):
    identity = post_identity(url)
    if not identity:
        raise InstagramCollectionError('Instagram post URL is invalid.')
    parser = MetadataParser()
    parser.feed(html)
    canonical = parser.meta.get('og:url')
    if canonical:
        other = post_identity(canonical)
        if not other or other[0] != identity[0]:
            raise InstagramCollectionError('Instagram post metadata does not match the requested post.')
    dom_published = optional_timestamp(timestamp)
    published = None
    raw_text = caption.strip()
    image = image_url
    images = []
    recognized = bool(caption or timestamp or canonical or parser.meta.get('og:description'))
    for root in parser.structured:
        for node in structured_nodes(root):
            types = node.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if not any(kind in types for kind in ('SocialMediaPosting', 'ImageObject', 'VideoObject', 'Article')):
                continue
            node_url = node.get('url') or node.get('mainEntityOfPage')
            if isinstance(node_url, str) and post_identity(node_url) and post_identity(node_url)[0] != identity[0]:
                continue
            recognized = True
            body = node.get('articleBody') or node.get('caption') or node.get('description')
            if not raw_text and isinstance(body, str):
                raw_text = body.strip()
            published = published or optional_timestamp(node.get('datePublished') or node.get('uploadDate'))
            if not image and 'ImageObject' in types:
                image = node.get('contentUrl', '')
            image_data = node.get('image') or node.get('thumbnailUrl')
            for value in (image_data if isinstance(image_data, list) else [image_data, node.get('contentUrl') if 'ImageObject' in types else None]):
                if isinstance(value, dict):
                    value = value.get('url') or value.get('contentUrl')
                if isinstance(value, str) and value.startswith('https://') and value not in images:
                    images.append(value)
            if not image and images:
                image = images[0]
            if not image and isinstance(image_data, str):
                image = image_data
            elif not image and isinstance(image_data, dict):
                image = image_data.get('url') or image_data.get('contentUrl') or ''
    raw_text = raw_text or parser.meta.get('og:description', '')
    published = published or optional_timestamp(parser.meta.get('article:published_time')) or dom_published
    image = image or parser.meta.get('og:image', '')
    if not recognized or not (raw_text or published or image):
        raise InstagramCollectionError('Expected Instagram post metadata/caption structures are absent.')
    if not isinstance(image, str) or urlparse(image).scheme not in ('https', 'http'):
        image = ''
    return CollectedSourceItem(
        external_id=identity[0], source_url=identity[1], raw_text=raw_text,
        published_at=published, raw_image_url=image,
        raw_metadata={'provider': 'playwright', 'shortcode': identity[0], 'source_username': username,
                      'post_kind': 'post' if '/p/' in identity[1] else ('reel' if '/reel/' in identity[1] else 'tv'),
                      'caption_origin': 'dom' if caption else 'metadata',
                      'published_at_available': published is not None,
                      'image_urls': list(dict.fromkeys([image, *images]))[:3] if image else images[:3]},
    )
