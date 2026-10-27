"""Local persistence and explicit, moderator-only candidate publication."""
from datetime import timedelta
from difflib import SequenceMatcher
import math
import re
import unicodedata
from zoneinfo import ZoneInfo

from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from events.models import Event
from events.moderation import LVIV_BOUNDS
from .public_images import publication_image, source_image_url
from .collectors import CollectedSourceItem
from .models import EventCandidate, RawSourceItem


def store_collected_item(source_account, item: CollectedSourceItem):
    """Idempotently persist raw content; never overwrite its first audit snapshot."""
    if not source_account.enabled:
        raise ValidationError('This source account is disabled.')
    raw = RawSourceItem(source_account=source_account, external_id=item.external_id,
        source_url=item.source_url, published_at=item.published_at, raw_text=item.raw_text,
        raw_image_url=item.raw_image_url, raw_metadata=item.raw_metadata)
    raw.full_clean(validate_unique=False, validate_constraints=False)
    return RawSourceItem.objects.get_or_create(
        source_account=source_account, external_id=item.external_id,
        defaults={field: getattr(raw, field) for field in (
            'source_url', 'published_at', 'raw_text', 'raw_image_url', 'raw_metadata',
        )},
    )


def normalize_fact(value):
    value = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(re.sub(r'[^\w\s]', ' ', value).split())


def find_possible_duplicate(candidate):
    """Same normalized title/address, or strong similarity within 30 minutes."""
    if candidate.start_time is None:
        return None
    title, address = normalize_fact(candidate.title), normalize_fact(candidate.address)
    if not title or not address:
        return None
    nearby = Event.objects.filter(
        start_time__gte=candidate.start_time - timedelta(minutes=30),
        start_time__lte=candidate.start_time + timedelta(minutes=30),
    ).exclude(status='rejected').exclude(pk=candidate.event_id).order_by('pk')
    for event in nearby:
        from .deduplication import match_level
        if match_level(candidate, event):
            return event
        other_title, other_address = normalize_fact(event.title), normalize_fact(event.address)
        exact = title == other_title and address == other_address
        similar = (SequenceMatcher(None, title, other_title).ratio() >= 0.9
                   and SequenceMatcher(None, address, other_address).ratio() >= 0.9)
        if exact or similar:
            return event
    return None


def build_candidate_description(candidate):
    """Generate original prose exclusively from factual fields, never from captions/drafts."""
    parts = [f'Подія: {candidate.title.strip()}.']
    venue = candidate.venue_name.strip()
    address = candidate.address.strip()
    if venue:
        parts.append(f'Місце: {venue}.')
    if address:
        parts.append(f'Адреса: {address}.')
    if candidate.start_time:
        start = candidate.start_time.astimezone(ZoneInfo('Europe/Kyiv'))
        parts.append(f'Початок: {start:%d.%m.%Y о %H:%M} (час Києва).')
    if candidate.end_time:
        end = candidate.end_time.astimezone(ZoneInfo('Europe/Kyiv'))
        parts.append(f'Завершення: {end:%d.%m.%Y о %H:%M} (час Києва).')
    if candidate.price_status == 'free':
        parts.append('Вхід безкоштовний.')
    elif candidate.price_status == 'paid' and candidate.price is not None:
        parts.append(f'Ціна: {candidate.price:.2f} ₴.')
    return ' '.join(parts)


def validate_approval(candidate):
    candidate.full_clean()
    errors = {}
    for field in ('title', 'address', 'category'):
        if not getattr(candidate, field).strip():
            errors[field] = 'Required before approval.'
    if candidate.start_time is None or not timezone.is_aware(candidate.start_time):
        errors['start_time'] = 'An explicit, timezone-aware start time is required.'
    elif candidate.start_time <= timezone.now():
        errors['start_time'] = 'Start must be in the future.'
    if candidate.end_time is not None:
        if not timezone.is_aware(candidate.end_time):
            errors['end_time'] = 'End must be timezone-aware.'
        elif (candidate.start_time and timezone.is_aware(candidate.start_time)
              and candidate.end_time < candidate.start_time):
            errors['end_time'] = 'End must be at or after start.'
    for field, minimum, maximum in (('latitude', -90, 90), ('longitude', -180, 180)):
        value = getattr(candidate, field)
        if value is None or not math.isfinite(value) or not minimum <= value <= maximum:
            errors[field] = 'A valid geographic coordinate is required.'
    if candidate.latitude is not None and candidate.longitude is not None:
        min_lon, min_lat, max_lon, max_lat = LVIV_BOUNDS
        if not min_lat <= candidate.latitude <= max_lat or not min_lon <= candidate.longitude <= max_lon:
            errors['latitude'] = 'Select a location within the supported Lviv bounds.'
    if candidate.confidence is not None and not math.isfinite(candidate.confidence):
        errors['confidence'] = 'Confidence must be finite.'
    if not candidate.public_photo and not (candidate.use_source_image_as_public_photo and source_image_url(candidate)):
        errors['public_photo'] = 'Upload a photo for publication or explicitly authorize the source image before approval.'
    if errors:
        raise ValidationError(errors)


def require_moderator(user, *, publish=False):
    if not user or not user.is_authenticated or not user.is_active or not user.is_staff:
        raise PermissionDenied('Candidate moderation requires an active staff account.')
    if not user.has_perm('ingestion.change_eventcandidate') or (publish and not user.has_perm('events.add_event')):
        raise PermissionDenied('Candidate review and Event creation permissions are required.')


def approve_candidate(candidate, reviewer):
    """Return (Event, created). Repeated approval is idempotent; likely duplicates are flagged."""
    require_moderator(reviewer, publish=True)
    duplicate = None
    stored_copy = None
    try:
        with transaction.atomic():
            # PostgreSQL is already required by this application's PostGIS model.
            # Serializing this small admin-only path also prevents cross-account
            # reposts from racing the duplicate check. No in-memory locks or workers.
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [742_691_001])
            record = EventCandidate.objects.select_for_update().get(pk=candidate.pk)
            if record.event_id:
                linked = Event.objects.select_for_update().get(pk=record.event_id)
                if linked.submitted_by_id and record.submitted_by_id and linked.submitted_by_id != record.submitted_by_id:
                    raise ValidationError('Candidate and Event have conflicting trusted owners.')
                if linked.status == 'approved' or record.raw_source_item_id:
                    return linked, False
            if record.status != EventCandidate.Status.NEW:
                raise ValidationError('Only new candidates may be approved. Return it to review after correcting facts.')
            validate_approval(record)
            duplicate = find_possible_duplicate(record)
            if duplicate:
                record.status = EventCandidate.Status.DUPLICATE
                record.reviewed_at, record.reviewed_by = timezone.now(), reviewer
                record.extraction_notes = (record.extraction_notes + f'\nPossible duplicate Event #{duplicate.pk}.').strip()
                record.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'extraction_notes', 'updated_at'])
                if record.event_id and not record.raw_source_item_id:
                    Event.objects.filter(pk=record.event_id).update(status='rejected')
            else:
                event = Event(
                    title=record.title.strip(), description=build_candidate_description(record) if record.raw_source_item_id else record.description.strip(),
                    start_time=record.start_time, end_time=record.end_time,
                    address=record.address.strip(), location=Point(record.longitude, record.latitude, srid=4326),
                    price=record.price, price_status=record.price_status, category=record.category,
                    source_url=record.source_url or (record.raw_source_item.source_url if record.raw_source_item_id else ''),
                    submitted_by_id=record.submitted_by_id,
                    photo=record.public_photo.name or 'authorized-source.jpg', status='approved',
                )
                created = record.event_id is None
                if not created:
                    linked = Event.objects.select_for_update().get(pk=record.event_id)
                    # Keep trusted ownership, organizer association, ID and submission clock.
                    for name in ('title', 'description', 'start_time', 'end_time', 'address', 'location',
                                 'price', 'price_status', 'category', 'source_url', 'photo', 'status'):
                        setattr(linked, name, getattr(event, name))
                    linked.submitted_by_id = linked.submitted_by_id or record.submitted_by_id
                    event = linked
                event.full_clean()
                if not record.public_photo:
                    filename, image_file = publication_image(record)
                    try:
                        event.photo.save(filename, image_file, save=False)
                        stored_copy = (event.photo.storage, event.photo.name)
                    except Exception:
                        raise ValidationError('Photo storage failed. Upload a public photo manually and try again.') from None
                event._skip_approval_notification = bool(record.raw_source_item_id)
                event.save()
                record.status = EventCandidate.Status.APPROVED
                record.event = event
                record.reviewed_at, record.reviewed_by = timezone.now(), reviewer
                record.save(update_fields=['status', 'event', 'reviewed_at', 'reviewed_by', 'updated_at'])
                from .models import EventSourceObservation
                EventSourceObservation.objects.filter(candidate=record).update(event=event)
                return event, created
    except Exception:
        if stored_copy:
            try:
                stored_copy[0].delete(stored_copy[1])
            except Exception:
                pass  # Storage cleanup must not mask the safe approval error.
        raise
    # Outside the committed transaction so the duplicate flag is not rolled back.
    raise ValidationError(f'Possible duplicate of Event #{duplicate.pk}; no public Event was created.')


def review_candidate(candidate, reviewer, status):
    require_moderator(reviewer)
    if status not in (EventCandidate.Status.NEW, EventCandidate.Status.REJECTED, EventCandidate.Status.DUPLICATE):
        raise ValidationError('Use the approval service to publish a candidate.')
    with transaction.atomic():
        record = EventCandidate.objects.select_for_update().get(pk=candidate.pk)
        if record.status == EventCandidate.Status.APPROVED or (record.event_id and (record.raw_source_item_id or record.event.status == 'approved')):
            raise ValidationError('Already published. Moderate the linked Event in Events admin.')
        if record.event_id:
            Event.objects.filter(pk=record.event_id).update(status='pending' if status == EventCandidate.Status.NEW else 'rejected')
        record.status = status
        record.reviewed_at = None if status == EventCandidate.Status.NEW else timezone.now()
        record.reviewed_by = None if status == EventCandidate.Status.NEW else reviewer
        record.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'updated_at'])
        return record
