"""Explicit retries of incomplete candidates; update identity-preserving blanks only."""
from copy import copy, deepcopy
from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from events.pricing import infer_price_status
from .models import EventCandidate, EventSourceObservation, RawSourceItem
from .provenance import moderator_edited_fields


def candidate_complete(candidate):
    if not candidate.title.strip() or candidate.start_time is None:
        return False
    try:
        candidate.full_clean(validate_constraints=False)
    except ValidationError:
        return False
    return True


def repair_plan(candidate, event):
    """Build a validated copy. Never mutate the candidate used for the preview."""
    from .extractors.location_evidence import location_supported
    from .extraction import event_identity
    from .deduplication import find_match
    protected = moderator_edited_fields(candidate)
    preview = copy(candidate)
    changes = {}
    for name in ('title', 'start_time', 'end_time', 'description', 'category', 'venue_name', 'address'):
        value = getattr(event, name)
        if getattr(candidate, name) not in (None, '') or value in (None, '') or name in protected:
            continue
        if name in ('venue_name', 'address') and event.location_provenance.get(name) not in ('event_specific', 'shared_post_context'):
            continue
        if name == 'address' and candidate.venue_name and event.venue_name and not location_supported(candidate.venue_name, event.venue_name):
            continue
        changes[name] = value
    if not {'price', 'price_status'} & protected:
        if candidate.price is None and event.price is not None:
            changes['price'] = event.price
            if candidate.price_status in ('', None, 'unknown'):
                changes['price_status'] = infer_price_status(event.price)
        elif candidate.price_status in ('', None):
            changes['price_status'] = infer_price_status(candidate.price)
        elif candidate.price_status == 'unknown' and candidate.price is not None and candidate.price == event.price:
            changes['price_status'] = infer_price_status(candidate.price)
    for name, value in changes.items():
        setattr(preview, name, value)
    reasons = {}
    if not preview.title:
        reasons['title'] = ['Missing title; moderator-owned blank cannot be filled.' if 'title' in protected else 'Missing or unsupported title.']
    if preview.start_time is None:
        reasons['start_time'] = ['Missing start; moderator-owned blank cannot be filled.' if 'start_time' in protected else 'Missing, ambiguous, or unsupported start time.']
    elif preview.start_time <= timezone.now():
        reasons['start_time'] = ['event start is in the past']
    if reasons:
        return None, {}, reasons
    if not preview.source_fingerprint:
        preview.source_fingerprint = event_identity(event)[0]
    try:
        preview.full_clean()
    except ValidationError as error:
        return None, {}, error.message_dict
    other, level = find_match(preview, preview.source_fingerprint)
    if other is not None:
        return None, {}, {'duplicate': [f'{level} match to {other._meta.label} #{other.pk}; moderator must compare existing identities before repair.']}
    observation = EventSourceObservation.objects.filter(raw_source_item_id=candidate.raw_source_item_id,
        fingerprint=preview.source_fingerprint).first()
    if observation and (observation.candidate_id != candidate.pk or observation.event_id != candidate.event_id):
        return None, {}, {'source_observation': ['Existing source observation belongs to another logical event; manual review required.']}
    return preview, changes, {}


def select_event(candidate, events, target_count):
    from .deduplication import title_similarity
    if len(events) == 1 and target_count == 1:
        return events[0]
    matches = [event for event in events
        if (not candidate.title or title_similarity(candidate.title, event.title) >= .85)
        and (not candidate.start_time or candidate.start_time == event.start_time)]
    return matches[0] if len(matches) == 1 and (candidate.title or candidate.start_time) else None


def repair_existing_candidates(raw_item, targets, *, provider, dry_run):
    from .extraction import ExtractionOutcome, extract_facts, provider_failure_code
    from .extractors.contracts import ExtractionBatch
    from .image_text import prepare_image_text
    previous = deepcopy(raw_item.raw_metadata.get('event_extraction'))
    # Explicit retry must bypass successful as well as historical fallback caches.
    raw_item.raw_metadata = deepcopy(raw_item.raw_metadata)
    raw_item.raw_metadata.pop('event_extraction', None)
    prepare_image_text(raw_item)
    try:
        result = extract_facts(raw_item, provider=provider)
    except Exception as error:
        result = None
        raw_item.raw_metadata['event_extraction'] = {'status': 'retry_failed',
            'fallback_reason': provider_failure_code(error)}
    audit = raw_item.raw_metadata.setdefault('event_extraction', {})
    audit['provider_retried'] = provider is not None
    configured_name = getattr(provider, 'name', None)
    configured_model = getattr(provider, 'model', None)
    audit['retry_provider'] = configured_name if isinstance(configured_name, str) else 'configured' if provider else 'deterministic'
    audit['retry_model'] = configured_model if isinstance(configured_model, str) else None
    if 'provider' not in audit:
        name = getattr(provider, 'name', None)
        audit['provider'] = name if isinstance(name, str) else 'configured' if provider is not None else 'deterministic'
    failure = audit.get('status') in ('fallback', 'retry_failed')
    events = list(result.events) if isinstance(result, ExtractionBatch) else [result] if result and result.is_event else []
    valid = [event for event in events if event.title and event.start_time and event.start_time > timezone.now()]
    unresolved = list(result.unresolved) if isinstance(result, ExtractionBatch) else []
    for event in events:
        if event.start_time and event.start_time <= timezone.now():
            unresolved.append({'title': event.title, 'reason': 'Historical occurrence not used for repair.',
                               'reasons': {'start_time': ['event start is in the past']}})
    proposals, used = [], set()
    for candidate in targets:
        event = select_event(candidate, valid, len(targets))
        if failure:
            reasons = {'provider': [audit.get('fallback_reason', 'provider_unavailable')]}
        elif event is None or id(event) in used:
            reasons = {'event': ['No unique valid upcoming event can be assigned to this candidate.']}
        else:
            _, _, reasons = repair_plan(candidate, event)
        if not reasons:
            used.add(id(event))
        proposals.append((candidate.pk, event, reasons))

    if isinstance(result, ExtractionBatch) and len(valid) > 1:
        for event in valid:
            if id(event) not in used:
                unresolved.insert(0, {'title': event.title, 'start_time': event.start_time.isoformat(),
                    'reason': 'Additional split event requires manual moderation; existing candidate identities preserved.',
                    'reasons': {'event_group': ['No unique existing candidate owns this additional poster event.']}})

    repairs, decisions = [], []
    def evaluate(candidate, event, reasons):
        if candidate.status != 'new' or candidate.event_id or candidate_complete(candidate):
            decisions.append({'title': candidate.title, 'decision': 'existing',
                'target': f'ingestion.EventCandidate #{candidate.pk}', 'reason': 'Complete/reviewed candidate preserved.'})
            return
        preview, changes, errors = (None, {}, reasons) if reasons else repair_plan(candidate, event)
        if errors:
            unresolved.append({'title': candidate.title, 'candidate_id': candidate.pk,
                'reason': 'Candidate retry did not produce a safe repair.', 'reasons': errors})
            return
        def primitive(value):
            return value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, Decimal) else value
        entry = {'candidate_id': candidate.pk, 'changes': {name: {'before': primitive(getattr(candidate, name)), 'after': primitive(value)} for name, value in changes.items()},
                 'complete': candidate_complete(preview), 'title': preview.title}
        repairs.append(entry)
        decisions.append({'title': preview.title, 'decision': 'repair', 'target': f'ingestion.EventCandidate #{candidate.pk}',
                          'reason': 'Would become complete.' if dry_run else 'Missing fields filled; candidate is complete.'})
        if dry_run:
            return
        for name, value in changes.items():
            setattr(candidate, name, value)
        if not candidate.source_fingerprint:
            candidate.source_fingerprint = preview.source_fingerprint
        candidate.extraction_notes = (candidate.extraction_notes + '\n[Repair] Filled missing fields: ' + ', '.join(changes)
            + ('\n[Repair evidence] ' + event.extraction_notes if event.extraction_notes else '')).strip()
        candidate.save(update_fields=[*changes, 'source_fingerprint', 'extraction_notes', 'updated_at'])
        facts = {name: getattr(candidate, name) for name in ('title', 'venue_name', 'address', 'category', 'price_status')}
        facts.update(start_time=candidate.start_time.isoformat(), price=str(candidate.price) if candidate.price is not None else None)
        EventSourceObservation.objects.get_or_create(raw_source_item=candidate.raw_source_item,
            fingerprint=candidate.source_fingerprint, defaults={'candidate': candidate, 'event': candidate.event,
                                                            'match_status': 'unique', 'facts': facts})

    if dry_run:
        for candidate_id, event, reasons in proposals:
            evaluate(next(candidate for candidate in targets if candidate.pk == candidate_id), event, reasons)
    else:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [742_691_001])
            current = RawSourceItem.objects.select_for_update().get(pk=raw_item.pk)
            for candidate_id, event, reasons in proposals:
                candidate = EventCandidate.objects.select_for_update().get(pk=candidate_id, raw_source_item=current)
                evaluate(candidate, event, reasons)
            history = list(current.raw_metadata.get('event_extraction_history', []))
            old = current.raw_metadata.get('event_extraction')
            if old is not None:
                history.append(deepcopy(old))
            audit.update(repairs=repairs, decisions=decisions, unresolved=unresolved,
                         candidate_ready=len(repairs), retry_failed=failure)
            current.raw_metadata = {**current.raw_metadata, **{key: value for key, value in raw_item.raw_metadata.items()
                if key.startswith('image_text') or key == 'poster_layout'}, 'event_extraction': audit, 'event_extraction_history': history}
            if repairs and all(candidate_complete(candidate) for candidate in current.candidates.all()):
                current.extraction_status = 'extracted'
            current.save(update_fields=['raw_metadata', 'extraction_status', 'updated_at'])
    audit.update(repairs=repairs, decisions=decisions, unresolved=unresolved, candidate_ready=len(repairs), retry_failed=failure)
    if previous is not None and dry_run:
        raw_item.raw_metadata['event_extraction_history'] = [*raw_item.raw_metadata.get('event_extraction_history', []), previous]
    return ExtractionOutcome('repair' if repairs else 'repair_failed' if unresolved else 'skipped',
        results=tuple(events), unresolved=tuple(unresolved), detected=len(events)+len(result.unresolved) if isinstance(result, ExtractionBatch) else len(events),
        ready=len(repairs), repaired=len(repairs), repairs=tuple(repairs), decisions=tuple(decisions),
        pricing_enriched=sum('price' in entry['changes'] or 'price_status' in entry['changes'] for entry in repairs))
