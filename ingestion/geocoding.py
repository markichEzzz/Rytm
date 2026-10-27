"""Bounded moderator searches, strict venue reuse, and private diagnostics."""
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
import json
import math
import re
import time
from typing import Protocol
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from events.models import Event
from events.moderation import LVIV_BOUNDS
from .models import EventCandidate, LocationLookup
from .addresses import canonical_address, canonical_known_address
from .provenance import moderator_edited_fields, address_source


class GeocodingError(Exception):
    pass


@dataclass(frozen=True)
class GeocodingResult:
    latitude: float
    longitude: float
    display_name: str = ''
    address_components: dict = field(default_factory=dict)
    address: str = ''


class GeocodingProvider(Protocol):
    def geocode(self, query: str) -> GeocodingResult | None: ...


class NominatimProvider:
    def __init__(self, *, wait_for_slot=False, persist_cache=True, require_address=False):
        self.wait_for_slot = wait_for_slot
        self.last_diagnostic = {}
        self.persist_cache = persist_cache
        self.require_address = require_address
        self.preview_cache = {}

    def geocode(self, query):
        endpoint = settings.GEOCODING_NOMINATIM_URL
        self.last_diagnostic = {'query': query, 'cache_hit': False, 'http_status': None, 'result_count': None, 'top_results': []}
        parsed = urlparse(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise GeocodingError('provider_unavailable')
        key = sha256((endpoint + '\n' + query.casefold()).encode()).hexdigest()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [742_691_002])
                if not cursor.fetchone()[0]:
                    raise GeocodingError('rate_limited')
            if key in self.preview_cache:
                data = self.preview_cache[key]
                self.last_diagnostic.update(data.get('diagnostic', {}), cache_hit=True)
                return self.unpack(data)
            cached = LocationLookup.objects.filter(pk=key).first()
            needs_upgrade = cached and self.require_address and not cached.result.get('error') and not cached.result.get('address_details_requested')
            if cached and not needs_upgrade and (not cached.result.get('error') or cached.looked_up_at > timezone.now() - timedelta(days=1)):
                self.last_diagnostic.update(cached.result.get('diagnostic', {}), cache_hit=True)
                if cached.result.get('error') == 'not_found' and not cached.result.get('diagnostic'):
                    self.last_diagnostic.update(result_count=0, legacy_cache=True)
                return self.unpack(cached.result)
            recent = LocationLookup.objects.order_by('-looked_up_at').first()
            delay = max(0, 1.1 - (timezone.now() - recent.looked_up_at).total_seconds()) if recent else 0
            if delay:
                if not self.wait_for_slot:
                    raise GeocodingError('rate_limited')
                time.sleep(min(delay, 1.1))  # One small wait between distinct staged searches, never retries.
            data = {'error': 'provider_unavailable'}
            try:
                started = time.monotonic()
                with requests.get(endpoint, params={
                    'q': query, 'format': 'jsonv2', 'limit': 2, 'countrycodes': 'ua', 'addressdetails': 1,
                    'viewbox': '23.90,49.90,24.15,49.77', 'bounded': 1,
                }, headers={'User-Agent': settings.GEOCODING_USER_AGENT},
                     timeout=(3, 7), allow_redirects=False, stream=True) as response:
                    self.last_diagnostic['http_status'] = response.status_code
                    if response.status_code != 200:
                        raise ValueError('HTTP failure')
                    body = bytearray()
                    for chunk in response.iter_content(8192):
                        if time.monotonic() - started > 20 or len(body) + len(chunk) > 65536:
                            raise ValueError('Response limit exceeded')
                        body.extend(chunk)
                    rows = json.loads(body)
                    if not isinstance(rows, list):
                        raise ValueError('Invalid result')
                    self.last_diagnostic['result_count'] = len(rows)
                    valid = []
                    for row in rows[:2]:
                        try:
                            components = row.get('address') or {}
                            components = {key: value[:255] for key, value in components.items() if key in ('road', 'house_number', 'city', 'town', 'village', 'postcode', 'country_code') and isinstance(value, str)} if isinstance(components, dict) else {}
                            result = GeocodingResult(float(row['lat']), float(row['lon']), str(row.get('display_name', ''))[:500], components)
                            accepted = in_bounds(result.latitude, result.longitude)
                            self.last_diagnostic['top_results'].append({'display_name': result.display_name,
                                'latitude': result.latitude if math.isfinite(result.latitude) else None,
                                'longitude': result.longitude if math.isfinite(result.longitude) else None, 'inside_bounds': accepted})
                            if accepted:
                                valid.append(result)
                        except (ValueError, TypeError, KeyError):
                            continue
                    if len(rows) > 2 or len(valid) > 1:
                        data = {'error': 'ambiguous'}
                    elif valid:
                        result = valid[0]
                        data = {'latitude': result.latitude, 'longitude': result.longitude, 'display_name': result.display_name, 'address_components': result.address_components}
                    else:
                        data = {'error': 'outside_supported_bounds' if rows else 'not_found'}
            except Exception:
                data = {'error': 'provider_unavailable'}
            data['address_details_requested'] = True
            data['diagnostic'] = {key: value for key, value in self.last_diagnostic.items() if key not in ('query', 'cache_hit')}
            if self.persist_cache:
                LocationLookup.objects.update_or_create(key=key, defaults={'query': query, 'result': data, 'looked_up_at': timezone.now()})
            else:
                self.preview_cache[key] = data
                # No persistent timestamp in previews: retain the application-wide
                # lock through the cooldown so another worker cannot request too soon.
                time.sleep(1.1)
        return self.unpack(data)

    @staticmethod
    def unpack(data):
        if data.get('error'):
            raise GeocodingError(data['error'])
        return GeocodingResult(data['latitude'], data['longitude'], data.get('display_name', ''), data.get('address_components', {}))


def in_bounds(latitude, longitude):
    min_lon, min_lat, max_lon, max_lat = LVIV_BOUNDS
    try:
        return math.isfinite(latitude) and math.isfinite(longitude) and min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon
    except (ValueError, TypeError):
        return False


def normalize_location_text(text):
    text = ' '.join((text or '').split()).strip(' ,;:!?|—–-')
    return re.sub(r'\b(вул|просп|пл|ім)\.(?=\S)', r'\1. ', text, flags=re.I)


def usable_address(text):
    # Do not assert that OCR fragments/gibberish describe a street. A recognisable
    # street prefix or a numeric house reference is enough to attempt a search.
    return bool(re.search(r'\d|\b(?:вул\.?|вулиця|просп\.?|проспект|пл\.?|площа|street|str\.?|avenue)\s', text, re.I))


def location_queries(candidate):
    address = normalize_location_text(candidate.address)
    venue = normalize_location_text(candidate.venue_name)
    base = []
    if address and usable_address(address):
        base.append(address)
        if venue:
            base.append(f'{venue}, {address}')
    if venue:
        base.append(venue)
        # Expand only two known abbreviations in SEARCH context, preserving facts.
        expanded = re.sub(r'\bДрамтеатр\b', 'театр', venue, flags=re.I)
        expanded = re.sub(r'\bім\.(?=\s|$)', 'імені', expanded, flags=re.I)
        if expanded != venue:
            base.append(expanded)
    elif address:
        base.append(address)
    return list(dict.fromkeys(f'{text[:550]}, Львів, Україна' for text in base))[:4]


def location_query(candidate):
    queries = location_queries(candidate)
    return queries[0] if queries else ''


def known_venue_location(candidate):
    from .services import normalize_fact
    venue = normalize_fact(candidate.venue_name)
    if not venue:
        return None
    address = normalize_fact(candidate.address) if usable_address(normalize_location_text(candidate.address)) else ''
    matches = []
    # Event has no venue field. Match the exact factual venue in its generated
    # description, never its title or a broad/fuzzy address search.
    for event in Event.objects.filter(status='approved').only('pk', 'description', 'address', 'location').order_by('-pk'):
        name = re.search(r'Місце:\s*(.*?)\.\s*(?:Адреса:|Початок:|$)', event.description)
        if not name or normalize_fact(name[1]) != venue:
            continue
        if address and normalize_fact(event.address) != address:
            continue
        if event.location and in_bounds(event.location.y, event.location.x):
            matches.append((event.location.y, event.location.x, canonical_known_address(event.address)))
    for other in EventCandidate.objects.exclude(pk=candidate.pk).filter(status='new', latitude__isnull=False, longitude__isnull=False):
        notes = [line for line in other.extraction_notes.splitlines() if line.startswith('[Location] ')]
        latest = notes[-1].casefold() if notes else ''
        if normalize_fact(other.venue_name) != venue or not latest.startswith(('[location] geocoded', '[location] known_venue')):
            continue
        if address and normalize_fact(other.address) != address:
            continue
        # Address-only matches do not establish the venue when addresses differ.
        if not address and 'from candidate address' in latest:
            continue
        if in_bounds(other.latitude, other.longitude):
            matches.append((other.latitude, other.longitude, canonical_known_address(other.address) if address_source(other) in ('geocoded', 'known_venue') else ''))
    if not matches:
        return None
    a, b, _ = matches[0]
    if any(((lat-a)*111000)**2 + ((lon-b)*72000)**2 > 75**2 for lat, lon, _ in matches):
        return None  # Conflicting locations for the exact same name require review.
    addresses = {value for _, _, value in matches if value}
    return GeocodingResult(a, b, 'Known verified venue', address=addresses.pop() if len(addresses) == 1 else '')


def location_note(candidate, message):
    note = '[Location] ' + message
    if not candidate.extraction_notes.endswith(note):
        candidate.extraction_notes = (candidate.extraction_notes + '\n' + note).strip()


@dataclass(frozen=True)
class LocationResolution:
    status: str
    message: str
    latitude: float | None = None
    longitude: float | None = None
    source: str = ''
    address: str = ''
    address_source: str = ''
    coordinates_enriched: bool = False
    address_enriched: bool = False
    already_complete: bool = False


def resolve_candidate_location(candidate, reviewer, provider=None):
    from .services import require_moderator
    require_moderator(reviewer)
    return enrich_candidate_location(candidate, provider=provider, preserve_partial=True).message


def nearby(first, second):
    return ((first.latitude - second.latitude) * 111000) ** 2 + ((first.longitude - second.longitude) * 72000) ** 2 <= 75 ** 2


def enrich_candidate_location(candidate, *, provider=None, dry_run=False, force=False,
                              preserve_partial=False, enrich_addresses=False):
    """Shared trusted resolver. Address-only enrichment never moves existing points.

    Force applies to coordinates; existing or moderator-cleared addresses always win.
    Dry runs also leave provider cache unchanged, including legacy cache upgrades.
    """
    from copy import copy
    with transaction.atomic():
        record = copy(candidate) if dry_run else EventCandidate.objects.select_for_update().get(pk=candidate.pk)
        if record.event_id or record.status != EventCandidate.Status.NEW:
            return LocationResolution('ineligible', 'Only unpublished candidates awaiting review can resolve a location.')
        complete = record.latitude is not None and record.longitude is not None
        partial = record.latitude is not None or record.longitude is not None
        notes = [line for line in record.extraction_notes.splitlines() if line.startswith('[Location] ')]
        manual = bool(notes and 'moderator selected or edited' in notes[-1].casefold())
        protected_address = 'address' in moderator_edited_fields(record)
        needs_address = not record.address.strip() and not protected_address
        def preserved(message):
            return LocationResolution('already_coordinates', message, record.latitude, record.longitude,
                address=record.address, address_source=address_source(record),
                already_complete=complete and bool(record.address.strip()))
        if not force and complete and (not enrich_addresses or not needs_address):
            return preserved('Existing coordinates preserved. Clear both and save only if another lookup is needed.')
        if not force and partial and not complete and (manual or preserve_partial):
            return preserved('Existing partial coordinates preserved; complete the moderator location manually.')
        if complete and not force and not in_bounds(record.latitude, record.longitude):
            return LocationResolution('failed', 'Existing point outside supported Lviv bounds; address requires manual review.')
        existing_point = GeocodingResult(record.latitude, record.longitude) if complete and not force else None
        known = known_venue_location(record)
        if known and existing_point and not nearby(existing_point, known):
            return LocationResolution('failed', 'Known venue disagrees with the existing verified/manual point; address not filled.')
        result = known
        source = 'known_venue'
        address = known.address if known else ''
        address_origin = 'known_venue'
        queries = location_queries(record)
        if not result and not queries:
            return LocationResolution('no_evidence', 'Add an address or venue and save before resolving location.')
        attempts, failure = [], 'not_found'
        # Known coordinates still resolve without another request in the original
        # coordinate-only workflow. The command explicitly requests address retries.
        search = not result or needs_address and enrich_addresses and not address
        engine = provider or NominatimProvider(wait_for_slot=True, persist_cache=not dry_run, require_address=needs_address)
        if search:
            for query in queries:
                lookup = None
                try:
                    lookup = engine.geocode(query)
                    if lookup is None:
                        raise GeocodingError('not_found')
                    if not in_bounds(lookup.latitude, lookup.longitude):
                        raise GeocodingError('outside_supported_bounds')
                    anchor = existing_point or known
                    if anchor and not nearby(anchor, lookup):
                        raise GeocodingError('location_mismatch')
                    address = canonical_address(lookup.address_components)
                    if not result:
                        result = lookup
                        source = 'geocoded'
                    if needs_address and address:
                        address_origin = 'geocoded'
                    failure = ''
                except GeocodingError as error:
                    failure = str(error)
                    lookup = None
                diagnostic = getattr(engine, 'last_diagnostic', {})
                attempts.append({**(diagnostic if isinstance(diagnostic, dict) else {}), 'query': query, 'status': failure or 'accepted'})
                if lookup:
                    break
                if failure in ('ambiguous', 'rate_limited', 'provider_unavailable', 'location_mismatch'):
                    break
        if attempts:
            record.extraction_notes = (record.extraction_notes + '\n[Location lookup] ' + json.dumps(attempts, ensure_ascii=False)).strip()
        errors = {
            'ambiguous': 'Ambiguous results; select location/address manually.',
            'outside_supported_bounds': 'Result outside supported Lviv bounds; select location manually.',
            'location_mismatch': 'Result disagrees with the existing verified/manual point; address not filled.',
            'rate_limited': 'Lookup rate limit reached; try again later or select location manually.',
            'not_found': 'No unique match found; select location manually.',
            'no_structured_address': 'No usable structured street address; manual address review required.',
        }
        coordinates_changed = bool(result and (not complete or force))
        address_changed = bool(result and needs_address and address)
        if address_changed:
            record.address = address
            origin = address_origin
            record.extraction_notes = (record.extraction_notes + '\n[Address] ' + origin + ': reliable road/house-number address.').strip()
        if coordinates_changed:
            record.latitude, record.longitude = result.latitude, result.longitude
            query = attempts[-1]['query'] if attempts else ''
            provenance = ('known_venue: reused verified coordinates for an exact venue match.' if source == 'known_venue' else
                'geocoded: from candidate address.' if query == f'{normalize_location_text(candidate.address)}, Львів, Україна' else
                'geocoded: from candidate venue search context' + ('; address still requires review.' if not record.address else '.'))
            location_note(record, provenance)
        if not coordinates_changed and not address_changed:
            failure = failure or 'no_structured_address'
            message = errors.get(failure, 'Geocoder unavailable; select location manually or retry later.')
            if complete:
                record.extraction_notes = (record.extraction_notes + '\n[Address review] ' + message).strip()
            else:
                location_note(record, message)
            if not dry_run:
                record.save(update_fields=['extraction_notes', 'updated_at'])
            return LocationResolution('ambiguous' if failure == 'ambiguous' else 'failed', message,
                record.latitude, record.longitude, address=record.address, address_source=address_source(record))
        if not dry_run:
            fields = ['extraction_notes', 'updated_at']
            if coordinates_changed:
                fields += ['latitude', 'longitude']
            if address_changed:
                fields.append('address')
            record.save(update_fields=fields)
        message = 'Location resolved using ' + ('verified venue coordinates' if source == 'known_venue' else 'OpenStreetMap Nominatim') + '. Verify the marker before approval.'
        if address_changed:
            message += ' Canonical address enriched.'
        elif needs_address:
            message += ' ' + errors.get(failure or 'no_structured_address', 'Address requires manual review.')
        return LocationResolution('resolved', message, record.latitude, record.longitude, source,
            record.address, address_source(record), coordinates_changed, address_changed)
