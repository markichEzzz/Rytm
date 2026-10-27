"""Provider-independent observations and evidence checks for semantic extraction."""
from dataclasses import asdict, replace
from datetime import time
import re
import unicodedata

from .contracts import EVIDENCE_FIELDS, KYIV, validate_result
from .parsers import MONTHS, WEEKDAYS, parse_times, parse_price, preprocess
from .location_evidence import location_supported, street_address, shared_scope, location_hints
from .categories import category_supported

CAPTION_LIMIT = 8000
OCR_LIMIT = 16000


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').replace('’', "'").replace('«', '"').replace('»', '"').replace('“', '"').replace('”', '"').casefold().split())


def observed_facts(caption, image, reference):
    observations = {}
    for source, original in (('caption', caption), ('ocr', image)):
        text, _ = preprocess(original)
        dates = set()
        pattern = (rf'\b\d{{1,2}}\s+(?:{"|".join(MONTHS)})(?:\s+\d{{4}})?\b'
                   r'|(?<!\w)\d{1,2}[./]\d{1,2}(?:[./](?:\d{4}|\d{2}))?(?!\w|[./]\d)'
                   rf'|\b(?:сьогодні|завтра|післязавтра|[ув]\s+(?:{"|".join(map(re.escape, WEEKDAYS))}))\b')
        for match in re.finditer(pattern, text.casefold()):
            try:
                start = parse_times(match[0] + ' 00:00', reference)[0]
                if start:
                    dates.add(start.date().isoformat())
            except Exception:
                continue
        clocks = set()
        for hour, minute in re.findall(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', text):
            try:
                clocks.add(time(int(hour), int(minute)).isoformat(timespec='minutes'))
            except ValueError:
                pass
        prices = set()
        for match in re.finditer(r'(?<![\d.,])(?:від\s+)?\d+(?:[ \u202f]\d{3})*(?:[.,]\d{1,2})?\s*(?:грн\b|₴|uah\b)|\bfree\b|безкоштовно|вхід\s+вільний', text, re.I):
            try:
                price = parse_price(match[0])[0]
                if price is not None:
                    prices.add(str(price))
            except Exception:
                pass
        observations[source] = {'dates': sorted(dates), 'times': sorted(clocks), 'prices_uah': sorted(prices)}
    return observations


def result_mapping(result):
    values = asdict(result)
    for name in ('start_time', 'end_time'):
        values[name] = getattr(result, name).isoformat() if getattr(result, name) else None
    values['price'] = str(result.price) if result.price is not None else None
    return values


def ground_semantic_result(result, context, *, multi_event=False):
    """Quoted source evidence must support every selected fact. Sanitize unsupported fields.

    This checks presence/date/clock/price, not semantic truth; moderator review remains
    mandatory. The model may select an evidenced primary event among several dates,
    but conflicts stay flagged locally regardless of its reported confidence.
    """
    result = validate_result(result)
    texts = {'caption': context.caption_text[:CAPTION_LIMIT], 'ocr': context.image_text[:OCR_LIMIT]}
    updates, evidence, issues, notes = {}, {}, [], []
    for name in EVIDENCE_FIELDS:
        value = getattr(result, name)
        entries = result.evidence.get(name, [])
        entries = [entry for entry in entries if (
            location_supported(entry['quote'], texts[entry['source']], ocr=entry['source'] == 'ocr')
            if name in ('venue_name', 'address') else normalized(entry['quote']) in normalized(texts[entry['source']]))]
        quotes = '\n'.join(entry['quote'] for entry in entries)
        supported = value is None
        if value is not None and entries:
            if name in ('venue_name', 'address'):
                supported = any(location_supported(value, entry['quote'], ocr=entry['source'] == 'ocr') for entry in entries)
                if name == 'address':
                    supported &= street_address(value)  # An explicitly named street need not include a house number.
            elif name == 'title':
                supported = normalized(value) in normalized(quotes)
            elif name == 'price':
                price, price_notes = parse_price(preprocess(quotes)[0])
                supported = price == value
                if supported:
                    notes.extend(price_notes)
            elif name in ('start_time', 'end_time'):
                start, end = evidenced_interval(quotes, context.reference_time) if multi_event else parse_times(preprocess(quotes)[0], context.reference_time)[:2]
                supported = value == (start if name == 'start_time' else end)
            elif name == 'category':
                supported = category_supported(value, quotes)
        if not supported:
            updates[name] = None
            issues.append(f'Unsupported {name} removed by local evidence validation.')
        elif value is not None:
            evidence[name] = entries
    if updates.get('start_time', result.start_time) is None:
        updates['end_time'] = None
        evidence.pop('end_time', None)
    observations = context.deterministic_observations.get('sources', {})
    dates = {date for facts in observations.values() for date in facts['dates']}
    prices = {price for facts in observations.values() for price in facts['prices_uah']}
    if len(dates) > 1 and not multi_event:
        issues.append('Multiple source dates detected; semantic selection requires manual review.')
    if len(prices) > 1 and not multi_event:
        issues.append('Multiple source prices detected; semantic selection requires manual review.')
    if not multi_event and 'conflict' in (context.deterministic_observations.get('result', {}).get('extraction_notes') or '').lower():
        issues.append('Caption/OCR deterministic conflict remains flagged for manual review.')
    if result.event_count is None or result.event_count > 1:
        issues.append('Multiple or unresolved event count; no automatic candidate selection.')
    issues = list(dict.fromkeys([*issues, *result.ambiguities]))[:12]
    confidence = min(result.confidence, .95)
    if issues or result.needs_review:
        confidence = min(confidence, .65)
    if any(entries and all(entry['source'] == 'ocr' for entry in entries) for entries in evidence.values()):
        confidence = min(confidence, .75)
        notes.append('OCR-only fields require verification against poster text.')
    if not updates.get('title', result.title) or not updates.get('start_time', result.start_time):
        confidence = min(confidence, .55)
    if not updates.get('category', result.category):
        confidence = min(confidence, .7)
    notes.extend(f'{name}: ' + '+'.join(sorted({entry['source'] for entry in entries})) for name, entries in evidence.items())
    provenance = {}
    for name in ('venue_name', 'address'):
        entries = evidence.get(name, [])
        if entries:
            scope = 'shared_post_context' if all(shared_scope(entry['quote']) for entry in entries) else (
                'ocr_only_uncertain' if all(entry['source'] == 'ocr' for entry in entries) else 'event_specific')
            if scope == 'ocr_only_uncertain':
                labelled = any(re.search(r'(?:локація|місце|venue|адреса|address)\s*[:–—-]', entry['quote'], re.I)
                               or name == 'address' and street_address(entry['quote']) for entry in entries)
                corroborated = any(hint['source'] == 'caption' and hint['field'] == name
                                   and location_supported(getattr(result, name), hint['value']) for hint in location_hints(context))
                if labelled or corroborated:
                    scope = 'event_specific'
                else:
                    updates[name] = None
                    evidence.pop(name, None)
                    notes.append(f'{name}: OCR-only uncertain location left unresolved.')
            provenance[name] = scope
            notes.append(f'{name}_provenance: {scope}.')
    # All LLM suggestions still enter moderator review, regardless of model flag.
    return validate_result(replace(result, **updates, evidence=evidence, location_provenance=provenance, confidence=confidence,
        needs_review=True, ambiguities=tuple(dict.fromkeys(issues))[:12],
        extraction_notes=' '.join(filter(None, [result.extraction_notes, *notes, *issues]))[:2000]))


def batch_mapping(batch):
    return {'is_event': batch.is_event, 'events': [
        {key: ({name: event.evidence.get(name, []) for name in EVIDENCE_FIELDS} if key == 'evidence' else value) for key, value in result_mapping(event).items()
         if key not in ('is_event', 'event_count', 'extraction_notes')}
        for event in batch.events], 'overall_notes': batch.overall_notes,
        'needs_review': batch.needs_review, 'shared_context': batch.shared_context or None}


def evidenced_interval(text, reference):
    """Support explicit multi-day ranges, never infer a missing ending clock."""
    from datetime import date, time
    from .parsers import localize
    lower = preprocess(text)[0].casefold()
    # Bare program day headings are relative to source publication, using the
    # same existing weekday resolver. This does not assign dates from a range.
    headings = {'понеділок': 'понеділок', 'вівторок': 'вівторок', 'середа': 'середу',
                'четвер': 'четвер', "п'ятниця": "п'ятницю", 'субота': 'суботу', 'неділя': 'неділю'}
    for heading, accusative in headings.items():
        lower = re.sub(r'(?m)^\s*' + re.escape(heading) + r'\s*(?=\n|$)', 'у ' + accusative + '\n', lower)
    english = dict(zip(('jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'), MONTHS))
    lower = re.sub(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b',
                   lambda match: english[match[0][:3]], lower)
    # Explicitly labelled former dates are not competing event dates.
    # Unlabelled/conflicting dates still remain ambiguous.
    lower = re.sub(r'\(\s*було\b[^)]*\)', '', lower)
    match = re.search(rf'\b(\d{{1,2}})\s*[–—-]\s*(\d{{1,2}})\s+({"|".join(MONTHS)})(?:\s+(\d{{4}}))?\b', lower)
    clocks = re.findall(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', lower)
    if match and clocks:
        year = int(match[4]) if match[4] else reference.astimezone(KYIV).year
        start = localize(date(year, MONTHS[match[3]], int(match[1])), time(*map(int, clocks[0])))
        end = localize(date(year, MONTHS[match[3]], int(match[2])), time(*map(int, clocks[1]))) if len(clocks) == 2 else None
        return start, end
    parts = re.split(r'\s+[–—]\s+', lower)
    if len(parts) == 2:
        start = parse_times(parts[0], reference)[0]
        end = parse_times(parts[1], reference)[0]
        if start and end:
            return start, end
    return parse_times(lower, reference)[:2]


def scoped_event_evidence(event, context):
    """Reject borrowing from a different clearly separated program paragraph.

    Shared fields need explicit source-wide wording. OCR layout without identifiable
    paragraph boundaries still needs human verification; presence is not semantic proof.
    """
    # Coordinate/logical groups enforce this at the final association boundary,
    # including explicit day headings outside a single paragraph.
    if context.poster_layout.get('groups'):
        return event
    evidence, notes = {}, []
    for name, entries in event.evidence.items():
        kept = []
        for entry in entries:
            text = context.caption_text if entry['source'] == 'caption' else context.image_text
            blocks = re.split(r'\n\s*\n', text)
            own = [block for block in blocks if event.title and normalized(event.title) in normalized(block)]
            shared = name in ('venue_name', 'address', 'price') and shared_scope(entry['quote'])
            if len(blocks) > 1 and own and not shared and not any(normalized(entry['quote']) in normalized(block) for block in own):
                notes.append(f'{name} evidence belongs to another program entry; removed.')
            else:
                kept.append(entry)
        evidence[name] = kept
    return replace(event, evidence=evidence, ambiguities=tuple([*notes, *event.ambiguities])[:12])
