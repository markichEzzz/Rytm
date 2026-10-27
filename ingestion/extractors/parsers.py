"""Conservative Ukrainian factual parsing; no usernames mapped to invented venues/addresses."""
from dataclasses import replace
from types import SimpleNamespace
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from decimal import Decimal
import re
import unicodedata

from django.utils import timezone

from .contracts import ExtractionError, ExtractionResult, KYIV
from .categories import CATEGORY_CUES, classify_category

MONTHS = dict(zip(('січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
                   'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня'), range(1, 13)))
WEEKDAYS = {'понеділок': 0, 'вівторок': 1, 'середу': 2, 'четвер': 3,
            "п'ятницю": 4, 'суботу': 5, 'неділю': 6}
EVENT_WORDS = r'концерт|\blive\b|вечірк|фестивал|виставк|стендап|stand.?up|театр|вистав[аи]|лекці|майстер.?клас|екскурсі|зустріч|зустріча|party'
EVENT_WORDS += '|' + '|'.join(CATEGORY_CUES[key] for key in ('education', 'performance', 'concert', 'party'))


def preprocess(text):
    text = unicodedata.normalize('NFKC', text or '').replace('’', "'").replace('\xa0', ' ')
    urls = re.findall(r'https?://\S+', text)
    return re.sub(r'https?://\S+', '', text).strip(), urls


def parse_price(text):
    pattern = r'(?<![\d.,])(?:(від)\s+)?(\d+(?:[ \u202f]\d{3})*(?:[.,]\d{1,2})?)\s*(?:грн\b|₴|uah\b)'
    matches = list(re.finditer(pattern, text, re.I))
    if any(text[:match.start()].rstrip().endswith('-') for match in matches):
        raise ExtractionError('Caption contains a negative price.')
    prices = {Decimal(match[2].replace(' ', '').replace('\u202f', '').replace(',', '.')) for match in matches}
    free = bool(re.search(r'\bfree\b|безкоштовн|вхід\s+вільний', text, re.I))
    if free and prices and prices != {Decimal(0)}:
        return None, ['Conflicting free/numeric prices; verify manually.']
    if len(prices) > 1:
        return None, ['Multiple prices; verify ticket tiers manually.']
    if prices:
        return prices.pop(), ['Minimum/base price (від); actual ticket price may differ.'] if any(m[1] for m in matches) else []
    return (Decimal(0), []) if free else (None, [])


def localize(day, clock):
    naive = datetime.combine(day, clock)
    candidates = [naive.replace(tzinfo=KYIV, fold=fold) for fold in (0, 1)]
    valid = [value for value in candidates if value.astimezone(utc_timezone.utc).astimezone(KYIV).replace(tzinfo=None) == naive]
    if not valid or (len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset()):
        return None
    return valid[0]


def parse_times(text, reference):
    lower = text.casefold()
    anchor = reference.astimezone(KYIV).date()
    dates, notes = [], []
    inferred = False
    weekday = False
    named = rf'\b(\d{{1,2}})\s+({"|".join(MONTHS)})(?:\s+(\d{{4}})(?:\s*року)?)?\b'
    numeric = r'(?<!\w)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\w|[./]\d)'
    matches = [(int(m[1]), MONTHS[m[2]], m[3]) for m in re.finditer(named, lower)]
    for match in re.finditer(numeric, lower):
        if re.match(r'\s*(грн|₴|uah)', lower[match.end():]):
            continue
        matches.append((int(match[1]), int(match[2]), match[3]))
    for day, month, year in matches:
        inferred |= year is None
        chosen_year = int(year) if year else anchor.year
        if chosen_year < 100:
            chosen_year += 2000
        # Only near New Year is an omitted-year rollover defensible; never advance an old October post a year.
        if not year and anchor.month == 12 and month == 1:
            chosen_year += 1
        try:
            dates.append(date(chosen_year, month, day))
        except ValueError:
            raise ExtractionError('Caption contains an invalid explicit date.') from None
    for word, offset in (('післязавтра', 2), ('завтра', 1), ('сьогодні', 0)):
        if re.search(rf'\b{word}\b', lower):
            dates.append(anchor + timedelta(days=offset))
    for word, number in WEEKDAYS.items():
        if re.search(rf'\b(?:[ув]\s+)?{re.escape(word)}\b', lower):
            dates.append(anchor + timedelta(days=(number - anchor.weekday()) % 7))
            weekday = True
    unique_dates = set(dates)
    if len(unique_dates) > 1:
        return None, None, ['Multiple event dates; manual selection required.'], True
    if inferred:
        notes.append('Year inferred from publication/reference date; verify manually.')
    if weekday:
        notes.append('Weekday resolved to nearest occurrence relative to publication; verify manually.')
    clocks = re.findall(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', lower)
    try:
        clocks = [time(int(hour), int(minute)) for hour, minute in clocks]
    except ValueError:
        raise ExtractionError('Caption contains an invalid explicit time.') from None
    if not unique_dates or not clocks:
        return None, None, notes + ['Date or time is unresolved; manual correction required.'], True
    day = unique_dates.pop()
    range_match = re.search(r'\d{1,2}:\d{2}\s*(?:[–—-]|до)\s*\d{1,2}:\d{2}', lower)
    if len(set(clocks)) > 1 and not (len(clocks) == 2 and range_match):
        return None, None, notes + ['Multiple times; manual selection required.'], True
    start = localize(day, clocks[0])
    end = None
    if range_match and len(clocks) == 2:
        end_day = day + timedelta(days=int(clocks[1] < clocks[0]))
        if clocks[1] != clocks[0]:
            end = localize(end_day, clocks[1])
        else:
            notes.append('Equal range endpoints; end time left unresolved.')
        if end_day != day:
            notes.append('Explicit time range crosses midnight.')
    if start is None or (range_match and end is None):
        return None, None, notes + ['Ambiguous/nonexistent DST time or range; manual correction required.'], True
    return start, end, notes, weekday


def parse_title(text):
    # A standalone artist/live line or explicit title label is more reliable than paraphrasing a caption.
    label = re.search(r'(?:назва|подія|концерт)\s*:\s*([^\n.!?]+)', text, re.I)
    if label:
        return label[1].strip()[:255]
    for line in text.splitlines():
        live = re.fullmatch(r'\s*([\w .\'-]{2,100})\s+live[.!]?\s*', line, re.I)
        if live:
            return live[1].strip()
    return None


def _extract_text_facts(raw_item, context):
    text, urls = preprocess(raw_item.raw_text)
    if not re.search(EVENT_WORDS, text, re.I):
        return ExtractionResult(False, extraction_notes='No concrete event signal found in deterministic mode.')
    start, end, notes, uncertain = parse_times(text, context.reference_time)
    title = parse_title(text)
    venue_match = re.search(r'(?:локація|місце|venue)\s*[:–—-]\s*([^\n;!?]+)', text, re.I)
    if not venue_match:
        venue_match = re.search(r'зустріча[\w]*\s+[ув]\s+([^\n.!?]+)', text, re.I)
    venue = venue_match[1].strip()[:255] if venue_match else None
    address_match = re.search(r'(?:адреса|address)\s*:\s*([^\n;]+)', text, re.I)
    if not address_match:
        address_match = re.search(r'((?:вул\.?|вулиц[яі]|просп\.?|проспект|пл\.?|площа)\s*[^\n;]+)', text, re.I)
    address = address_match[1].strip()[:255] if address_match else None
    price, price_notes = parse_price(text)
    notes += price_notes
    category = classify_category(text)
    if not title:
        notes.append('Title unresolved; supply it in admin.')
    if not category:
        notes.append('Category unresolved; existing choices only.')
    if not address:
        notes.append('No explicit address; no geocoding or account-based inference.')
    if not raw_item.published_at:
        notes.append('Publication time unavailable; current time used as date context.')
    if urls:
        notes.append('Caption URLs detected; original raw post URL retained as provenance.')
    score = (0.5 if start else 0) + (0.2 if title else 0) + (0.1 if venue else 0) + (0.1 if category else 0) + (0.1 if price is not None else 0)
    if uncertain:
        score = min(score, 0.55 if start is None else 0.7)
    if start and start <= timezone.now():
        return ExtractionResult(False, start_time=start, end_time=end, extraction_notes='Explicit/resolved event date is in the past; not an upcoming candidate.')
    return ExtractionResult(True, title=title, start_time=start, end_time=end, venue_name=venue,
                            address=address, price=price, category=category, confidence=round(score, 2),
                            extraction_notes=' '.join(notes))



def source_texts(raw_item):
    """Keep caption and private poster text as distinct evidence sources."""
    return raw_item.raw_text or '', (raw_item.raw_metadata or {}).get('image_text', '') or ''


def deterministic_extract(raw_item, context):
    caption, image = source_texts(raw_item)
    if not image:
        return _extract_text_facts(raw_item, context)
    # Poster layout title is a conservative review hint, not an account-derived fact.
    poster = image
    lines = [line.strip() for line in image.splitlines() if line.strip() and not re.fullmatch(r'IMAGE \d+:', line.strip())]
    title_hint = None
    if lines and re.fullmatch(r"[A-ZА-ЯІЇЄҐ][A-ZА-ЯІЇЄҐ .'’\-]{2,99}", lines[0]):
        title_hint = lines[0]
        poster = 'Подія: ' + title_hint + '\n' + image
    combined = SimpleNamespace(raw_text=caption + '\n' + poster, published_at=raw_item.published_at)
    # An image with explicit title, date and clock is also concrete event evidence.
    image_start = parse_times(preprocess(poster)[0], context.reference_time)[0]
    if title_hint and image_start and not re.search(EVENT_WORDS, combined.raw_text, re.I):
        combined.raw_text += '\nПодія зустріч'
    result = _extract_text_facts(combined, context)
    if not result.is_event:
        return result
    left = _extract_text_facts(SimpleNamespace(raw_text=caption, published_at=raw_item.published_at), context)
    right = _extract_text_facts(SimpleNamespace(raw_text=poster + '\nзустріч', published_at=raw_item.published_at), context)
    notes = [result.extraction_notes, 'Caption and private image OCR were inspected separately; verify OCR accuracy.']
    updates = {}
    for field in ('title', 'venue_name', 'address', 'category'):
        a, b = getattr(left, field), getattr(right, field)
        if a and b and ' '.join(a.casefold().split()) != ' '.join(b.casefold().split()):
            updates[field] = None
            notes.append(f'Caption/image conflict in {field}; manual review required.')
    if any('Multiple' in note or 'Conflicting' in note for note in notes if note) or updates:
        updates['confidence'] = min(result.confidence, 0.4)
        notes.append('Conflicting caption/image evidence remains unresolved.')
    if title_hint:
        updates.setdefault('confidence', min(result.confidence, 0.7))
        notes.append('Poster heading used as title; verify layout manually.')
    # Only an actual OCR line equal to the configured source name is a venue hint.
    names = {' '.join(value.casefold().split()) for value in (context.source_username, context.source_display_name) if value}
    venue_lines = [line for line in lines[1:] if ' '.join(line.casefold().split()) in names]
    if not result.venue_name and len(set(venue_lines)) == 1 and 'venue_name' not in updates:
        updates['venue_name'] = venue_lines[0][:255]
        notes.append('Venue name explicitly present on poster; verify its role manually.')
    return replace(result, **updates, extraction_notes=' '.join(filter(None, notes))[:2000])
