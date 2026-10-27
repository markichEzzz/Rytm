"""Shared format cues for deterministic classification and quoted LLM evidence."""
import re

CATEGORY_CUES = {
    'education': r'майстер[ -]?клас|мастерклас|воркшоп|\bworkshops?\b|лекці|\blectures?\b|навчання|заняття|\bкурс\w*|\bшкол\w*|\bclasses?\b|educational',
    'performance': r'опер[аиу]|комеді[яї]|вистав[аиу]|театр|спектакл|кінопоказ|\bкіно\b|фільм|\bperformance\b|\bscreenings?\b|\bmovie\b|\bdocumentary\b|\btheatre\b|\bplay\b',
    'concert': r'концерт|\bconcert\b|\bjazz\b|джаз|\borchestra\b|оркестр|симфонічн|\bband\b|\bгурт\w*|\blive\s+music\b|\bacoustic\s+live\b',
    'party': r'вечірк|\bparty\b|\bafterparty\b|\bdj\b|\brave\b|рейв|\bclub\s+night\b|\bnightlife\b|стендап|stand.?up|\bcomedy\b',
    'museums': r'виставк|музе[йю]|exhibition|museum',
    'routes': r'екскурсі|excursion|\btour\b',
    'restaurants': r'ресторан|їж[аі]|food|restaurant',
    'nature': r'природ|парк|nature|park',
}


def category_supported(category, text):
    return bool(re.search(CATEGORY_CUES.get(category, r'(?!)'), text, re.I))


def classify_category(text):
    if category_supported('education', text):
        return 'education'
    concert = category_supported('concert', text)
    party = category_supported('party', text)
    if concert and party:
        # An ancillary afterparty does not turn the advertised concert into nightlife.
        if re.search(r'(?:головн\w*|основн\w*|main|primary)\s+(?:подія\s*[:—-]?\s*)?(?:afterparty|афтерпаті|вечірк)', text, re.I):
            return 'party'
        if re.search(r'afterparty|афтерпаті', text, re.I):
            return 'concert'
        # A heading provides a stronger format signal than later supporting copy.
        heading = next((line for line in text.splitlines() if line.strip()), '')
        first = re.search(CATEGORY_CUES['concert'] + '|' + CATEGORY_CUES['party'], heading, re.I)
        if first:
            return 'concert' if category_supported('concert', first[0]) else 'party'
        return None
    if concert:
        return 'concert'
    if party:
        return 'party'
    for category in ('performance', 'museums', 'routes', 'restaurants', 'nature'):
        if category_supported(category, text):
            return category
    return None
