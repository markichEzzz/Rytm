"""Bounded collection of public Telegram channel HTML; no sessions or authentication."""
from datetime import datetime
from html.parser import HTMLParser
import re
import time
from urllib.parse import urlparse

import requests
from ingestion.collectors import CollectedSourceItem


class TelegramCollectionError(Exception):
    pass


class Node:
    def __init__(self, tag='', attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()

    def text(self):
        return ''.join(child.text() if isinstance(child, Node) else child for child in self.children)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag == 'br':
            node.children.append('\n')
        if tag == 'img':
            node.children.append(node.attrs.get('alt', ''))
        if tag not in ('br','img','meta','link','input','hr','source','wbr'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_public_page(html, username, limit):
    parser = PageParser()
    try:
        parser.feed(html)
    except Exception:
        raise TelegramCollectionError('parsing_failed') from None
    posts = [node for node in parser.root.walk() if node.attrs.get('data-post')]
    if not posts:
        if any(node.has_class('tgme_channel_info') for node in parser.root.walk()):
            return []
        raise TelegramCollectionError('access_blocked_or_unavailable')
    items = {}
    for post in posts:
        identifier = post.attrs['data-post']
        if not re.fullmatch(re.escape(username) + r'/[0-9]+', identifier, re.I):
            continue
        nodes = list(post.walk())
        text = '\n'.join(n.text().strip() for n in nodes if n.has_class('tgme_widget_message_text'))[:24000]
        published = None
        for node in nodes:
            if node.tag == 'time':
                try:
                    value = datetime.fromisoformat(node.attrs.get('datetime','').replace('Z','+00:00'))
                    if value.tzinfo:
                        published = value
                except ValueError:
                    pass
                break
        images = []
        for node in nodes:
            if node.has_class('tgme_widget_message_photo_wrap'):
                match = re.search(r"background-image\s*:\s*url\(['\"]?([^)'\"]+)", node.attrs.get('style',''))
                if match:
                    url = match[1]
                    host = urlparse(url).hostname or ''
                    if url.startswith('https://') and (host == 'cdn4.telesco.pe' or host.endswith('.telesco.pe')):
                        images.append(url)
        external_id = identifier.split('/')[1]
        items[external_id] = CollectedSourceItem(external_id=external_id,
            source_url=f'https://t.me/{username}/{external_id}', raw_text=text, published_at=published,
            raw_image_url=images[0] if images else '', raw_metadata={'platform':'telegram', 'message_id':external_id, 'image_urls':list(dict.fromkeys(images))[:3]})
    if not items:
        raise TelegramCollectionError('parsing_failed')
    return sorted(items.values(), key=lambda item: int(item.external_id), reverse=True)[:limit]


class TelegramCollector:
    def __init__(self, limit=5):
        if not 1 <= limit <= 20:
            raise TelegramCollectionError('limit_must_be_1_to_20')
        self.limit = limit

    def fetch_items(self, source_account):
        username = source_account.username
        if not re.fullmatch(r'[A-Za-z0-9_]{5,150}', username):
            raise TelegramCollectionError('invalid_public_channel_username')
        try:
            with requests.get(f'https://t.me/s/{username}', timeout=(5,10), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise TelegramCollectionError('access_blocked_or_unavailable')
                body = bytearray()
                started = time.monotonic()
                for chunk in response.iter_content(16384):
                    if len(body)+len(chunk) > 2_000_000 or time.monotonic()-started > 20:
                        raise TelegramCollectionError('response_limit_exceeded')
                    body.extend(chunk)
                return parse_public_page(body.decode('utf-8', errors='replace'), username, self.limit)
        except TelegramCollectionError:
            raise
        except requests.RequestException:
            raise TelegramCollectionError('public_channel_unavailable') from None

    def close(self):
        pass
