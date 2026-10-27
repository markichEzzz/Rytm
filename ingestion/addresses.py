"""Concise addresses from structured roads; never parse a geocoder display_name."""
import re

STREET_TYPES = {'вулиця': 'вул.', 'проспект': 'просп.', 'площа': 'пл.', 'провулок': 'пров.'}


def canonical_address(components):
    if not isinstance(components, dict) or not isinstance(components.get('road'), str):
        return ''
    road = ' '.join(components['road'].split()).strip()
    if not road or len(road) > 255 or re.search(r'[,;\n]|https?://', components['road'], re.I):
        return ''
    for full, short in STREET_TYPES.items():
        if road.casefold().startswith(full + ' '):
            road = short + ' ' + road[len(full):].strip()
            break
        if road.casefold().endswith(' ' + full):
            road = short + ' ' + road[:-len(full)].strip()
            break
    house = components.get('house_number')
    house = house.strip() if isinstance(house, str) else ''
    if not re.fullmatch(r'\d+[A-Za-zА-Яа-яІіЇїЄєҐґ]?(?:\s*[-–/]\s*\d+[A-Za-zА-Яа-яІіЇїЄєҐґ]?)*', house):
        house = ''
    result = road + (', ' + house if house else '')
    return result if len(result) <= 255 else ''


def canonical_known_address(text):
    if not isinstance(text, str):
        return ''
    parts = text.strip().split(',')
    if len(parts) > 2 or len(parts) == 2 and not re.fullmatch(r'\s*\d+[\w/\-– ]*\s*', parts[1]):
        return ''
    return canonical_address({'road': parts[0], 'house_number': parts[1].strip() if len(parts) == 2 else ''})
