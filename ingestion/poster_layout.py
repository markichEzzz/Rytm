"""Bounded OCR layout context and conservative event-group association checks.

Coordinates are source pixels, never location data. Groups are evidence hints, not
extracted events. This module never invents a title, date, venue or price.
"""
from dataclasses import replace
from statistics import median
import math
import re

from .extractors.hybrid import normalized
from .extractors.parsers import MONTHS, WEEKDAYS
from .extractors.location_evidence import shared_scope

MAX_BLOCKS = 240
MAX_LAYOUT_TEXT = 12000
MAX_GROUPS = 50
LOW_CONFIDENCE = 45
CLOCK = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)')
MONTH_WORDS = [*MONTHS, 'jan', 'january', 'feb', 'february', 'mar', 'march', 'apr', 'april', 'may', 'jun', 'june', 'jul', 'july', 'aug', 'august', 'sep', 'september', 'oct', 'october', 'nov', 'november', 'dec', 'december']
DAY_WORDS = [*WEEKDAYS, 'середа', "п'ятниця", 'п’ятниця', 'субота', 'неділя']
DATE = re.compile(r'\b\d{1,2}\s*(?:[–—-]\s*\d{1,2}\s*)?(?:' + '|'.join(sorted(MONTH_WORDS, key=len, reverse=True)) + r')(?:\s+\d{4})?\b|(?<!\d)\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b(?:' + '|'.join(map(re.escape, DAY_WORDS)) + r')\b', re.I)



def clean_blocks(image):
    """Allowlist editable metadata; enforce geometry/text bounds before provider input."""
    if not isinstance(image, dict):
        return [], False
    blocks, size, truncated = [], 0, False
    values = image.get('blocks', [])
    if not isinstance(values, list):
        return [], False
    for value in values[:MAX_BLOCKS + 1]:
        if len(blocks) >= MAX_BLOCKS or size >= MAX_LAYOUT_TEXT:
            truncated = True
            break
        if not isinstance(value, dict) or not isinstance(value.get('text'), str):
            continue
        text = value['text'].strip()[:500]
        geometry = [value.get(key) for key in ('x', 'y', 'width', 'height')]
        if not text or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0 or n > 100000 for n in geometry) or min(geometry[2:]) <= 0:
            continue
        confidence = value.get('confidence')
        confidence = float(confidence) if not isinstance(confidence, bool) and isinstance(confidence, (int, float)) and math.isfinite(confidence) and 0 <= confidence <= 100 else None
        minimum = value.get('confidence_min', confidence)
        minimum = float(minimum) if not isinstance(minimum, bool) and isinstance(minimum, (int, float)) and math.isfinite(minimum) and 0 <= minimum <= 100 else confidence
        size += len(text)
        blocks.append(dict(text=text, **dict(zip(('x', 'y', 'width', 'height'), geometry)), confidence=confidence, confidence_min=minimum, order=len(blocks)))
    return sorted(blocks, key=lambda block: (block['y'], block['x'])), truncated or len(values) > MAX_BLOCKS


def _groups(blocks, image_index, width):
    if not blocks:
        return []
    anchors = [b for b in blocks if DATE.search(b['text']) or CLOCK.search(b['text'])]
    xs = sorted({b['x'] for b in anchors})
    # A substantial gap between temporal anchor columns is required; indentation is not a column.
    columns = [[]]
    centers = []
    for x in xs:
        if not centers or x - centers[-1][-1] > max(120, width * .24):
            centers.append([x])
        else:
            centers[-1].append(x)
    if len(centers) > 1:
        positions = [median(values) for values in centers]
        columns = [[] for _ in positions]
        for block in blocks:
            if block['width'] > width * .75 and shared_scope(block['text']):
                continue
            index = min(range(len(positions)), key=lambda i: abs(block['x'] - positions[i]))
            columns[index].append(block)
    else:
        columns = [blocks]
    groups = []
    for column in columns:
        chunks, chunk = [], []
        threshold = max(28, median([b['height'] for b in column]) * 2.2) if column else 28
        for block in column:
            if chunk and block['y'] - (chunk[-1]['y'] + chunk[-1]['height']) > threshold:
                chunks.append(chunk)
                chunk = []
            chunk.append(block)
        if chunk:
            chunks.append(chunk)
        date_context = ''
        for chunk in chunks:
            clocks = [b for b in chunk if CLOCK.search(b['text'])]
            # Inline schedule rows have their own title; standalone clocks cannot be
            # assigned to neighboring titles purely by nearest distance.
            inline = len(clocks) > 1 and all(len(DATE.sub('', CLOCK.sub('', b['text'])).strip(' –—-:')) >= 3 for b in clocks)
            if inline:
                current = []
                for block in chunk:
                    if DATE.search(block['text']) and not CLOCK.search(block['text']):
                        if current:
                            groups.append(_group(current, image_index, date_context))
                            current = []
                        date_context = block
                    elif CLOCK.search(block['text']):
                        if current:
                            groups.append(_group(current, image_index, date_context))
                        current = [block]
                    elif current:
                        current.append(block)
                if current:
                    groups.append(_group(current, image_index, date_context))
            elif clocks or any(DATE.search(b['text']) for b in chunk):
                group = _group(chunk, image_index, date_context)
                if len(clocks) > 1:
                    group['warnings'].append('Several standalone time anchors have uncertain title associations.')
                groups.append(group)
                headings = [b for b in chunk if DATE.search(b['text']) and not CLOCK.search(b['text'])]
                if headings:
                    date_context = headings[-1]
    return groups


def _group(blocks, image_index, date_context=''):
    text = '\n'.join(block['text'] for block in blocks)
    warnings = []
    if sum(bool(re.search(r'(?:концерт|подія|вистава|event)\s*[:–—]', b['text'], re.I)) for b in blocks) > 1 and len(CLOCK.findall(text)) <= 1:
        warnings.append('Several event titles share an unassigned time anchor.')
    for i, first in enumerate(blocks):
        for second in blocks[i + 1:]:
            intersection = max(0, min(first['x'] + first['width'], second['x'] + second['width']) - max(first['x'], second['x'])) * max(0, min(first['y'] + first['height'], second['y'] + second['height']) - max(first['y'], second['y']))
            if intersection > .3 * min(first['width'] * first['height'], second['width'] * second['height']):
                warnings.append('Overlapping OCR lines make event association uncertain.')
                break
    return {'image_index': image_index, 'source': 'ocr', 'text': text,
            'date_context': date_context['text'] if isinstance(date_context, dict) else date_context,
            'date_context_confidence': date_context.get('confidence_min', date_context.get('confidence')) if isinstance(date_context, dict) else None,
            'blocks': blocks, 'warnings': list(dict.fromkeys(warnings))}


def poster_context(raw_item):
    metadata = raw_item.raw_metadata or {}
    images, groups, warnings, budget = [], [], [], MAX_LAYOUT_TEXT
    layout = metadata.get('image_text_layout', [])
    for image_index, image in enumerate(layout[:3] if isinstance(layout, list) else [], 1):
        blocks, truncated = clean_blocks(image)
        kept = []
        for block in blocks:
            if len(block['text']) > budget:
                truncated = True
                break
            budget -= len(block['text'])
            kept.append(block)
        width = max((b['x'] + b['width'] for b in kept), default=1)
        images.append({'image_index': image.get('image_index', image_index) if type(image.get('image_index')) is int else image_index, 'blocks': kept})
        groups.extend(_groups(kept, images[-1]['image_index'], width))
        if truncated or image.get('truncated') is True:
            warnings.append('Structured OCR context truncated; some events may be omitted.')
    caption = (raw_item.raw_text or '')[:8000]
    ocr = metadata.get('image_text', '')
    ocr = ocr[:16000] if isinstance(ocr, str) else ''
    for source, text in [('caption', caption), ('ocr', ocr if not images else '')]:
        paragraphs = [re.sub(r'^IMAGE \d+:\s*', '', p.strip()) for p in re.split(r'\n\s*\n', text) if p.strip()]
        sections = []
        for paragraph in paragraphs:
            labels = list(re.finditer(r'(?im)^(?:концерт|подія|вистава|event)\s*[:–—]', paragraph))
            if len(labels) > 1:
                sections.extend(paragraph[match.start():labels[i+1].start() if i+1 < len(labels) else len(paragraph)].strip() for i, match in enumerate(labels))
            else:
                sections.append(paragraph)
        groups.extend({'source': source, 'image_index': None, 'text': p[:3000], 'date_context': '', 'blocks': [], 'warnings': []}
                      for p in sections if DATE.search(p) and CLOCK.search(p))
        if not any(g['source'] == source for g in groups):
            # Text-only day headings plus inline schedule rows: logical reading order,
            # explicitly not OCR coordinates or an invented date assignment.
            lines = [line.strip() for line in text.splitlines() if line.strip()][:240]
            logical = _groups([dict(text=line[:500], x=0, y=i*24, width=800, height=20, confidence=None, order=i)
                               for i, line in enumerate(lines)], None, 1000)
            if len(logical) > 1:
                groups.extend({**g, 'source': source, 'blocks': []} for g in logical)
    unique = {}
    for group in groups:
        unique.setdefault((group['source'], normalized(group['text']), normalized(group['date_context'])), group)
    groups = list(unique.values())[:MAX_GROUPS]
    for index, group in enumerate(groups, 1):
        group['event_group_index'] = index
    text = caption + '\n' + ocr
    dates = set(normalized(m[0]) for m in DATE.finditer(text))
    clocks = set(CLOCK.findall(text))
    temporal_groups = [g for g in groups if CLOCK.search(g['text'])]
    group_count = max((sum(g['source'] == source for g in temporal_groups) for source in ('caption', 'ocr')), default=0)
    suspected = group_count > 1 or len(clocks) >= 3 or len(dates) > 1 and len(clocks) > 1
    # An explicitly advertised single range/festival is context, not proof of separate events.
    range_only = len(dates) == 1 and any(re.search(r'\d\s*[–—-]\s*\d', m[0]) for m in DATE.finditer(text))
    if range_only and len(temporal_groups) <= 1 and len(clocks) <= 2:
        suspected = False
    if suspected and not groups:
        warnings.append('Multiple temporal anchors without reliable layout groups; verify associations manually.')
    confidence = [b['confidence'] for image in images for b in image['blocks'] if b['confidence'] is not None]
    return {'version': 1, 'detected_multi_event_poster': suspected,
            'ocr_block_count': sum(len(image['blocks']) for image in images), 'images': images,
            'groups': groups, 'warnings': list(dict.fromkeys(warnings)),
            'ocr_confidence_summary': {'mean': round(sum(confidence) / len(confidence), 1), 'minimum': min(b['confidence_min'] for image in images for b in image['blocks'] if b['confidence_min'] is not None)} if confidence else None}


def associate_event(event, context):
    """Select one supported group; remove fields borrowed from another poster entry."""
    layout = context.poster_layout
    groups = layout.get('groups', [])
    own = [g for g in groups if event.title and normalized(event.title) in normalized(g['text'])]
    exact = [g for g in own if any(normalized(re.sub(r'^(?:концерт|подія|вистава|event)\s*[:–—]\s*', '', line, flags=re.I)) == normalized(event.title) for line in g['text'].splitlines())]
    if exact:
        own = exact
    title_sources = {entry['source'] for entry in event.evidence.get('title', [])}
    if len(title_sources) == 1:
        preferred = [g for g in own if g['source'] in title_sources]
        if preferred:
            own = preferred
    if len(own) > 1:
        date_quotes = [entry['quote'] for entry in event.evidence.get('start_time', [])]
        matches = [g for g in own if date_quotes and all(normalized(q) in normalized(g['text'] + '\n' + g['date_context']) for q in date_quotes)]
        own = matches
    chosen = own[0] if len(own) == 1 else None
    issues, evidence, blocked = [], {}, set()
    multi = layout.get('detected_multi_event_poster', False)
    for field, entries in event.evidence.items():
        kept = []
        for entry in entries:
            quote = entry['quote']
            shared = field in ('venue_name', 'address', 'price') and shared_scope(quote)
            if shared:
                low = entry['source'] == 'ocr' and any(b['confidence_min'] is not None and b['confidence_min'] < LOW_CONFIDENCE and
                    (normalized(quote) in normalized(b['text']) or normalized(b['text']) in normalized(quote))
                    for image in layout.get('images', []) for b in image['blocks'])
                if low:
                    issues.append(f'{field}: shared OCR evidence has low confidence.')
                else:
                    kept.append(entry)
                continue
            if chosen and entry['source'] == chosen['source']:
                scope = chosen['text'] + ('\n' + chosen['date_context'] if field in ('start_time', 'end_time') else '')
                supported = normalized(quote) in normalized(scope)
                low = [b for b in chosen['blocks'] if (normalized(quote) in normalized(b['text']) or normalized(b['text']) in normalized(quote)) and b['confidence_min'] is not None and b['confidence_min'] < LOW_CONFIDENCE]
                if supported and not low:
                    kept.append(entry)
                else:
                    issues.append(f'{field}: different event group or low-confidence OCR; left unresolved.')
            elif entry['source'] == 'caption':
                # Complementary caption details need the same title's paragraph, or explicit shared wording.
                caption_groups = [g for g in groups if g['source'] == 'caption' and event.title and normalized(event.title) in normalized(g['text'])]
                scopes = [g['text'] + '\n' + g['date_context'] for g in caption_groups]
                if not scopes:
                    scopes = [p for p in re.split(r'\n\s*\n', context.caption_text) if event.title and normalized(event.title) in normalized(p)
                              and len(set(CLOCK.findall(p))) <= 2 and len(set(DATE.findall(p))) <= 1]
                supported = any(normalized(quote) in normalized(scope) for scope in scopes)
                if supported or not multi:
                    kept.append(entry)
                else:
                    issues.append(f'{field}: caption does not establish this event association.')
            elif not multi and not chosen:
                kept.append(entry)
            else:
                issues.append(f'{field}: no unique OCR event group; left unresolved.')
        evidence[field] = kept
        if entries and not kept:
            blocked.add(field)
    if chosen and chosen.get('date_context_confidence') is not None and chosen['date_context_confidence'] < LOW_CONFIDENCE and not DATE.search(chosen['text']):
        issues.append('Shared date heading has low OCR confidence; day association requires review.')
        blocked.add('start_time')
    if chosen and multi and re.search(r'\d\s*[–—-]\s*\d', chosen['date_context']) and not DATE.search(chosen['text']):
        issues.append('Shared date range does not assign an individual day to this event.')
        blocked.add('start_time')
    if chosen and chosen['warnings']:
        issues.extend(chosen['warnings'])
        if any(e['source'] == 'ocr' for e in evidence.get('start_time', [])):
            blocked.add('start_time')
    if multi and not chosen and any(e['source'] == 'ocr' for entries in event.evidence.values() for e in entries):
        issues.append('No unique spatial/logical event group could be identified.')
    if 'start_time' in blocked:
        blocked.add('end_time')
    updates = {field: None for field in blocked}
    notes = []
    if chosen:
        notes.append(f"[Poster] group={chosen['event_group_index']} image={chosen['image_index'] or '-'} source={chosen['source']}.")
    notes.extend(issues)
    return replace(event, **updates, evidence=evidence, needs_review=True,
                   confidence=min(event.confidence, .55) if issues else event.confidence,
                   ambiguities=tuple(dict.fromkeys([*event.ambiguities, *issues]))[:12],
                   extraction_notes=' '.join(filter(None, [event.extraction_notes, *notes]))[:2000]), chosen
