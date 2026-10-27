"""Deterministic submission policy; content review remains available in admin."""

from datetime import timedelta
from decimal import Decimal
import math

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, Throttled

from .models import Event, Organizer

SUBMISSION_COOLDOWN = timedelta(hours=3)
LVIV_BOUNDS = (23.90, 49.77, 24.15, 49.90)  # min longitude/latitude, max longitude/latitude


def should_auto_approve(event, organizer, *, now=None):
    """Only linked, active, verified organizers with complete Lviv events qualify.

    API-invalid data is rejected by the serializer before this policy runs.
    A valid shorter description, location outside Lviv, or untrusted account
    goes to the manual queue. Cooldown is enforced separately under a row lock.
    """
    now = now or timezone.now()
    if organizer is None or not organizer.verified or organizer.user is None:
        return False
    user = organizer.user
    if not user.is_authenticated or not user.is_active:
        return False
    point = event.get('location')
    start = event.get('start_time')
    end = event.get('end_time')
    price = event.get('price')
    min_lon, min_lat, max_lon, max_lat = LVIV_BOUNDS
    return all((
        5 <= len(event.get('title', '').strip()) <= 255,
        20 <= len(event.get('description', '').strip()) <= 10000,
        len(event.get('address', '').strip()) >= 5,
        event.get('category') in dict(Event.CATEGORY_CHOICES),
        start is not None and start > now,
        end is None or (start is not None and end > start),
        price is None or (price.is_finite() and price >= Decimal('0')),
        point is not None and math.isfinite(point.x) and math.isfinite(point.y)
        and min_lon <= point.x <= max_lon and min_lat <= point.y <= max_lat,
        bool(event.get('photo')),
    ))


def create_submission(serializer, user):
    # Lock the account even for its first submission: an event-only lock would
    # have nothing to lock yet and concurrent requests could both succeed.
    with transaction.atomic():
        account = get_user_model().objects.select_for_update().get(pk=user.pk)
        if not account.is_active:
            raise PermissionDenied('This organizer account is inactive.')
        now = timezone.now()
        latest = Event.objects.filter(submitted_by=account).order_by('-created_at').first()
        if latest is not None:
            remaining = latest.created_at + SUBMISSION_COOLDOWN - now
            if remaining.total_seconds() > 0:
                seconds = math.ceil(remaining.total_seconds())
                minutes = math.ceil(seconds / 60)
                hours, minutes = divmod(minutes, 60)
                message = f'You can submit another event in {hours}h {minutes}m.'
                error = Throttled(wait=seconds)
                # Keep the standard JSON detail and Retry-After header without
                # appending a second, confusing wait message.
                error.detail = message
                raise error
        organizer = Organizer.objects.select_related('user').filter(user=account).first()
        result = 'approved' if should_auto_approve(
            serializer.validated_data, organizer, now=now,
        ) else 'pending'
        event = serializer.save(
            status=result, organizer=organizer, submitted_by=account, created_at=now,
        )

        # Reuse the existing Event submission row to preserve API IDs and cooldown.
        # Source provenance is nullable for human submissions; no fake source user.
        from ingestion.models import EventCandidate
        EventCandidate.objects.create(
            submitted_by=account, event=event, title=event.title, description=event.description,
            start_time=event.start_time, end_time=event.end_time, address=event.address,
            latitude=event.location.y, longitude=event.location.x, price=event.price,
            price_status=event.price_status, category=event.category, source_url=event.source_url or '',
            public_photo=event.photo.name, created_at=now,
            status='approved' if result == 'approved' else 'new',
            reviewed_at=now if result == 'approved' else None,
        )
        return event
