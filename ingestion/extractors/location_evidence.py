"""Conservative location hints and scope checks, independent of account identity."""
import re
import unicodedata


def location_tokens(text):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    text = re.sub(r'\bдрамтеатр\b', 'театр', text)
    text = re.sub(r'\bім\.\s*', 'імені ', text)
    text = re.sub(r'\b(?:вул\.|вулиці)\s*', 'вулиця ', text)
    text = re.sub(r'\bпросп\.\s*', 'проспект ', text)
    # Correct mixed-script lookalikes only inside predominantly Cyrillic words.
    lookalikes = str.maketrans('aceiopxy', 'асеіорхУ'.casefold())
    words = re.findall(r'[\w]+', text)
    return [word.translate(lookalikes) if len(re.findall('[а-яіїєґ]', word)) > len(word) / 2 else word for word in words]


def same_location_word(left, right, *, noise=False):
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    stem = lambda word: re.sub(r'(?:ої|ій|ого|ому|а|у|і|ї|ю|я|и)$', '', word)
    if len(stem(left)) >= 5 and stem(left) == stem(right):
        return True
    # At most one OCR substitution, and only with another exact anchoring word.
    return noise and min(len(left), len(right)) >= 8 and len(left) == len(right) and sum(a != b for a, b in zip(left, right)) == 1


def location_supported(value, quote, *, ocr=False):
    wanted, observed = location_tokens(value), location_tokens(quote)
    if not wanted:
        return False
    for index in range(len(observed) - len(wanted) + 1):
        span = observed[index:index + len(wanted)]
        anchor = len(wanted) > 1 and any(a == b and len(a) >= 4 for a, b in zip(wanted, span))
        if all(same_location_word(a, b, noise=ocr and anchor) for a, b in zip(wanted, span)):
            return True
    return False


def street_address(text):
    return bool(re.search(r'\b(?:вул\.?|вулиц[яі]|просп\.?|проспект|пл\.?|площа|street|avenue)\s*\S', text or '', re.I))


def shared_scope(quote):
    # Explicit program/calendar framing, not a sponsor/footer/logo mention.
    return bool(re.search(
        r'(?:усі|всі|all)\s+(?:події|концерти|events|concerts).*?(?:\b[ув]|\bна|\bat)\s+'
        r'|(?:поді[їй]|концерти|афіша|програма|events|concerts|schedule).*?(?:\b[ув]|\bна|\bat)\s+',
        quote or '', re.I))


def location_hints(context):
    hints = []
    for source, text in (('caption', context.caption_text[:8000]), ('ocr', context.image_text[:16000])):
        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) > 500:
                continue
            shared = shared_scope(line)
            venue = re.search(r'(?:локація|місце|venue)\s*[:–—-]\s*(.+)', line, re.I)
            if not venue and shared:
                parts = re.split(r'\b(?:[ув]|на|at)\s+', line, flags=re.I)
                venue = re.search(r'(.+)', parts[-1]) if len(parts) > 1 else None
            if venue:
                value = re.split(r'\s*[;|]\s*|,\s*(?=вул|просп|пл)', venue[1], maxsplit=1)[0].strip(' .!,:;()')
                if value and len(value) <= 255:
                    hints.append({'field': 'venue_name', 'value': value, 'source': source, 'quote': line,
                                  'scope': 'shared_post_context' if shared else 'event_specific' if source == 'caption' else 'ocr_only_uncertain'})
            address = re.search(r'(?:вул\.?|вулиц[яі]|просп\.?|проспект|пл\.?|площа)\s*[^\n;|]+', line, re.I)
            if address:
                value = address[0].strip(' .!,:;')[:255]
                hints.append({'field': 'address', 'value': value, 'source': source, 'quote': line,
                              'scope': 'shared_post_context' if shared else 'event_specific' if source == 'caption' else 'ocr_only_uncertain'})
    return hints[:50]


def event_location_hints(event, context):
    from dataclasses import replace
    from .hybrid import normalized
    hints = []
    for source, text in (('caption', context.caption_text), ('ocr', context.image_text)):
        for block in re.split(r'\n\s*\n', text):
            if not event.title or normalized(event.title) not in normalized(block):
                continue
            own_lines = '\n'.join(line for line in block.splitlines() if normalized(event.title) in normalized(line))
            # A multi-entry paragraph must use its own line, not a sibling's address.
            dated_lines = re.findall(r'\d{1,2}[./]\d{1,2}|\d{1,2}\s+(?:жовтня|листопада|грудня)', block, re.I)
            own = own_lines if len(dated_lines) > 1 else block
            scoped = replace(context, caption_text=own if source == 'caption' else '', image_text=own if source == 'ocr' else '')
            hints.extend(hint for hint in location_hints(scoped) if hint['scope'] != 'shared_post_context')
    return hints


def validated_shared_context(shared, context):
    from .contracts import EVIDENCE_FIELDS, ExtractionResult
    from .hybrid import ground_semantic_result
    fields = {}
    for field in ('venue_name', 'address'):
        entries = [entry for entry in (shared or {}).get('evidence', {}).get(field, []) if shared_scope(entry['quote'])]
        suggestions = [hint for hint in location_hints(context) if hint['field'] == field and hint['scope'] == 'shared_post_context']
        if len({tuple(location_tokens(hint['value'])) for hint in suggestions}) > 1:
            continue  # Multiple program locations cannot establish one post-wide venue.
        value = (shared or {}).get(field)
        if not value and len({tuple(location_tokens(hint['value'])) for hint in suggestions}) == 1:
            value = suggestions[0]['value']
            entries = [{'source': hint['source'], 'quote': hint['quote']} for hint in suggestions[:4]]
        evidence = {name: [] for name in EVIDENCE_FIELDS}
        evidence[field] = entries
        result = ground_semantic_result(ExtractionResult(True, **{field: value}, event_count=1, evidence=evidence), context, multi_event=True)
        if getattr(result, field):
            fields[field] = getattr(result, field)
            fields.setdefault('evidence', {})[field] = result.evidence[field]
    if fields:
        return {'venue_name': fields.get('venue_name'), 'address': fields.get('address'),
                'evidence': {name: fields.get('evidence', {}).get(name, []) for name in ('venue_name', 'address')}}
    return {}
