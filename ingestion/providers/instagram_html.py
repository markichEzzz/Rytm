"""Small Instagram HTML helpers. No full page HTML or session data is persisted."""
from datetime import datetime
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlparse

from django.utils import timezone
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError

from ingestion.collectors import CollectedSourceItem
from .instagram import InstagramCollectionError

POST_PATH = re.compile(r'^/(?:([A-Za-z0-9_.]+)/)?(p|reel|reels)/([A-Za-z0-9_-]+)/?$')


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
    kind = 'p' if match[2] == 'p' else 'reel'
    return match[3], f'https://www.instagram.com/{kind}/{match[3]}/'


def recent_post_links(links, limit, username=None, *, allow_collaborations=False):
    result, seen = [], set()
    for href in links:
        if not isinstance(href, str):
            continue
        try:
            parsed = urlparse(urljoin('https://www.instagram.com/', href))
        except ValueError:
            continue
        match = POST_PATH.fullmatch(parsed.path)
        if username and not allow_collaborations and match and match[1] and match[1].casefold() != username.casefold():
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
    unique = {record.get('datetime') for record in records if record.get('datetime') and not record.get('href')}
    return next(iter(unique)) if len(unique) == 1 else None


def post_dom_candidates(page):
    """Caption-bearing semantic elements only; never arbitrary spans/comments/body text."""
    caption = ''
    for selector in ('[role="dialog"] h1, [role="dialog"] [itemprop="caption"], [role="dialog"] [itemprop="description"]',
                     'article h1, main h1, [role="main"] h1',
                     'article [itemprop="caption"], article [itemprop="description"], main [data-testid="post-caption"]'):
        values = page.locator(selector).all_text_contents()
        caption = next((value.strip() for value in values if isinstance(value, str) and value.strip()), '')
        if caption:
            break
    records = page.locator('time[datetime]').evaluate_all(
        '(elements) => elements.map(e => ({datetime: e.getAttribute("datetime"), href: e.closest("a")?.getAttribute("href")}))')
    return caption, records


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


def optional_media_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        URLValidator(schemes=['https', 'http'])(value)
        if urlparse(value).username or urlparse(value).password:
            return ''
    except (ValidationError, ValueError):
        return ''
    return value


def normalize_post_html(url, html, *, caption='', timestamp=None, image_url='', username=''):
    identity = post_identity(url)
    if not identity:
        raise InstagramCollectionError('Instagram post URL is invalid.')
    parser = MetadataParser()
    parser.feed(html)
    canonical = parser.meta.get('og:url')
    if canonical:
        other = post_identity(canonical)
        if other and other[0] != identity[0]:
            raise InstagramCollectionError('Instagram post metadata does not match the requested post.')
        if other is None:
            canonical = None  # Generic page/profile metadata is not a post identity.
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
    raw_text = raw_text or parser.meta.get('og:description', '') or parser.meta.get('description', '')
    published = published or optional_timestamp(parser.meta.get('article:published_time')) or dom_published
    image = image or parser.meta.get('og:image:secure_url', '') or parser.meta.get('og:image', '')
    recognized = recognized or bool(raw_text)
    images = list(dict.fromkeys(value for candidate in [image, *images] if (value := optional_media_url(candidate))))[:3]
    image = images[0] if images else ''
    if not recognized or not (raw_text or published or image):
        raise InstagramCollectionError('Expected Instagram post metadata/caption structures are absent.')
    return CollectedSourceItem(
        external_id=identity[0], source_url=identity[1], raw_text=raw_text,
        published_at=published, raw_image_url=image,
        raw_metadata={'provider': 'playwright', 'shortcode': identity[0], 'source_username': username,
                      'post_kind': 'post' if '/p/' in identity[1] else 'reel',
                      'caption_origin': 'dom' if caption else 'metadata',
                      'published_at_available': published is not None,
                      'image_urls': list(dict.fromkeys([image, *images]))[:3] if image else images[:3]},
    )


def post_candidate_diagnostics(url, html='', *, caption='', timestamp=None):
    """Presence flags only; never return HTML, caption values or private media URLs."""
    identity = post_identity(url)
    parser = MetadataParser()
    parser.feed(html)
    nodes = [node for root in parser.structured for node in structured_nodes(root)]
    return {'post_kind': ('post' if '/p/' in identity[1] else 'reel') if identity else 'unknown',
            'shortcode_parsed': identity is not None,
            'caption_candidate_found': bool(caption or parser.meta.get('og:description') or parser.meta.get('description')
                or any(node.get('articleBody') or node.get('caption') or node.get('description') for node in nodes)),
            'timestamp_candidate_found': bool(timestamp or parser.meta.get('article:published_time')
                or any(node.get('datePublished') or node.get('uploadDate') for node in nodes)),
            'media_candidate_found': bool(parser.meta.get('og:image') or parser.meta.get('og:image:secure_url')
                or any(node.get('image') or node.get('thumbnailUrl') or node.get('contentUrl') for node in nodes)),
            'path_class': ('/p/<shortcode>/' if '/p/' in identity[1] else '/reel/<shortcode>/') if identity else 'unsupported'}
