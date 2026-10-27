"""Extraction orchestration: facts to private candidates, never approval or publication."""
from dataclasses import dataclass, replace
import logging
import os

from django.db import IntegrityError, transaction
from django.utils import timezone

from .extractors.contracts import (
    ExtractionContext, ExtractionError, ExtractionResult, EventExtractionProvider, ExtractionBatch, KYIV, validate_result,
)
from .extractors.parsers import deterministic_extract, parse_price, preprocess, source_texts
from .extractors.hybrid import observed_facts, result_mapping, ground_semantic_result, batch_mapping, scoped_event_evidence
from .extractors.location_evidence import location_hints, validated_shared_context, event_location_hints
from .image_text import prepare_image_text
from .poster_layout import poster_context, associate_event
from .models import EventCandidate, RawSourceItem
from .services import build_candidate_description
from events.pricing import infer_price_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionOutcome:
    action: str
    result: ExtractionResult | None = None
    candidate: EventCandidate | None = None
    error: str | None = None
    results: tuple = ()
    candidates: tuple = ()
    unresolved: tuple = ()
    detected: int = 0
    ready: int = 0
    duplicates: int = 0
    matched: int = 0
    enriched: int = 0
    pricing_enriched: int = 0
    linked: int = 0
    decisions: tuple = ()
    repaired: int = 0
    repairs: tuple = ()


def provider_failure_code(error):
    """Only predefined diagnostic codes; never log provider bodies/exception text."""
    from .extractors.openai import ProviderUnavailable
    codes = {'missing_api_key', 'missing_model', 'provider_http_failure', 'provider_response_limit',
             'provider_incomplete_or_refused', 'provider_invalid_output', 'provider_invalid_schema',
             'provider_timeout', 'provider_invalid_or_unavailable', 'unknown_provider',
             'ollama_invalid_base_url', 'ollama_model_not_found', 'ollama_http_failure',
             'ollama_response_limit', 'ollama_incomplete_output', 'ollama_invalid_json',
             'ollama_validation_failure', 'ollama_timeout', 'ollama_unavailable',
             'ollama_invalid_or_unavailable'}
    if isinstance(error, ProviderUnavailable) and str(error) in codes:
        return str(error)
    return 'provider_validation_failure' if isinstance(error, ExtractionError) else 'provider_unavailable'


def persistence_decision(candidate, existing, identities, identity, *, dry_run):
    """Read-only plan reused before preview and again inside the persistence lock."""
    from .deduplication import find_match
    old = match_existing_location_event(existing, identities, candidate.source_fingerprint, identity)
    other, level = (old, 'existing') if old is not None else (None, None)
    if other is None and candidate.raw_source_item_id:
        from .models import EventSourceObservation
        observation = EventSourceObservation.objects.filter(raw_source_item_id=candidate.raw_source_item_id,
            fingerprint=candidate.source_fingerprint).select_related('candidate', 'event').first()
        if observation and (observation.candidate_id or observation.event_id):
            other, level = observation.candidate or observation.event, 'confirmed'
    if other is None:
        other, level = find_match(candidate, candidate.source_fingerprint)
    details = {'title': candidate.title, 'fingerprint': candidate.source_fingerprint,
               'decision': 'existing' if level == 'existing' else 'link' if level == 'confirmed' else 'review' if level == 'probable' else 'create'}
    if other is not None:
        details['target'] = f'{other._meta.label} #{other.pk}'
    if level == 'probable':
        details['reason'] = 'Ambiguous/probable duplicate; moderator must compare observations.'
    else:
        # Check all model fields in both paths. New rows additionally validate their
        # conditional fingerprint uniqueness; existing/link targets are reused.
        candidate.full_clean(validate_constraints=other is None,
            exclude=['raw_source_item'] if dry_run and candidate.raw_source_item_id is None else None)
        details['reason'] = 'Existing candidate preserved.' if level == 'existing' else 'Confident duplicate linked.' if level == 'confirmed' else 'Valid candidate.'
    return details, other


def configured_provider():
    """Explicit configuration only; unavailable adapters fall back per raw item."""
    name = os.environ.get('EVENT_EXTRACTION_PROVIDER', 'openai').strip().lower()
    if name in ('', 'deterministic'):
        return None
    from .extractors.openai import OpenAIEventExtractionProvider, UnavailableProvider
    if name == 'openai':
        return OpenAIEventExtractionProvider()
    if name == 'ollama':
        from .extractors.ollama import OllamaEventExtractionProvider
        return OllamaEventExtractionProvider()
    return UnavailableProvider('unknown_provider')


def extract_facts(raw_item, provider: EventExtractionProvider | None = None):
    context = ExtractionContext(
        reference_time=(raw_item.published_at or timezone.now()).astimezone(KYIV),
        source_username=raw_item.source_account.username,
        source_display_name=raw_item.source_account.display_name,
        caption_text=raw_item.raw_text, image_text=source_texts(raw_item)[1],
        current_time=timezone.now().astimezone(KYIV),
    )
    layout = poster_context(raw_item)
    context = replace(context, poster_layout=layout)
    raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'poster_layout': layout}
    deterministic = validate_result(deterministic_extract(raw_item, context))
    context = replace(context, deterministic_observations={
        'sources': observed_facts(context.caption_text[:8000], context.image_text[:16000], context.reference_time),
        'result': result_mapping(deterministic),
        'location_hints': location_hints(context),
    })
    result = deterministic
    if provider is None and layout['detected_multi_event_poster']:
        return deferred_poster(raw_item, deterministic)
    if provider is None and deterministic.start_time and deterministic.start_time <= timezone.now():
        return deterministic
    if provider is not None:
        legacy_result = False
        try:
            result = provider.extract(raw_item, context)
            if isinstance(result, dict):
                result = ExtractionBatch.from_mapping(result) if 'events' in result else ExtractionResult.from_mapping(result)
            if isinstance(result, ExtractionBatch):
                if not result.events and not result.unresolved and deterministic.start_time and deterministic.start_time <= timezone.now():
                    # A provider may omit historical events. Preserve the independently
                    # parsed past-date diagnostic without inventing a future suggestion.
                    result = replace(result, unresolved=({
                        'title': None, 'start_time': deterministic.start_time.isoformat(),
                        'reason': 'No upcoming event; caption contains an explicit past date.',
                        'reasons': {'start_time': ['event start is in the past']},
                        'price': None, 'price_status': 'unknown',
                    },))
                return prepare_batch(raw_item, result, context)
            result = validate_result(result)
            legacy_result = result.event_count is None and not result.evidence and not result.ambiguities
            if not legacy_result:
                result, _ = associate_event(result, context)
                result = ground_semantic_result(result, context)
        except Exception as error:
            legacy_result = False
            # Never log exception text, response bodies, or credentials.
            logger.warning('Raw item #%s: semantic provider unavailable or invalid; deterministic fallback.', raw_item.pk)
            result = replace(deterministic, needs_review=True, extraction_notes=' '.join(filter(None, [
                deterministic.extraction_notes, 'Semantic provider unavailable or invalid; deterministic fallback.',
            ]))[:2000])
            raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'event_extraction': {
                'provider': 'deterministic', 'status': 'fallback', 'result': result_mapping(result),
                'fallback_reason': provider_failure_code(error),
                'provider_http_status': getattr(error, 'http_status', None) if type(getattr(error, 'http_status', None)) is int else None,
                'attempted_provider': getattr(provider, 'name', None) if isinstance(getattr(provider, 'name', None), str) else None,
                'attempted_model': getattr(provider, 'model', None) if isinstance(getattr(provider, 'model', None), str) else None,
                'provider_response_seconds': getattr(provider, 'response_seconds', None) if isinstance(getattr(provider, 'response_seconds', None), (int, float)) else None,
                'deterministic_result': result_mapping(deterministic),
            }}
        if layout['detected_multi_event_poster'] and (legacy_result or (raw_item.raw_metadata or {}).get('event_extraction', {}).get('status') == 'fallback'):
            return deferred_poster(raw_item, deterministic)
        if legacy_result and result.is_event:
            # Legacy providers: explicit deterministic facts take precedence; providers handle the less certain semantics.
            values = {field: getattr(deterministic, field) for field in (
                'start_time', 'end_time', 'title', 'venue_name', 'address', 'category',
            ) if getattr(deterministic, field) is not None}
            text, _ = preprocess("\n".join(source_texts(raw_item)))
            price, notes = parse_price(text)
            if 'conflict' in (deterministic.extraction_notes or '').lower():
                values.update({field: getattr(deterministic, field) for field in ('title', 'start_time', 'end_time', 'venue_name', 'address', 'category')})
                values['confidence'] = min(result.confidence, deterministic.confidence)
            values['price'] = price  # Never infer a price not explicitly stated.
            values['extraction_notes'] = ' '.join(filter(None, [
                deterministic.extraction_notes, result.extraction_notes, *notes,
            ]))[:2000]
            result = validate_result(replace(result, **values))
    if result.start_time and result.start_time <= timezone.now() and (result.event_count == 1 or result.event_count is None and not result.ambiguities):
        result = replace(result, is_event=False, event_count=0 if result.event_count is not None else None, extraction_notes='Resolved event date is in the past; not an upcoming candidate.')
    if result.is_event:
        incomplete = not result.start_time or not result.title
        result = replace(result, confidence=min(result.confidence, 0.55) if incomplete else result.confidence)
        # Even provider summaries are regenerated; public descriptions also use this factual boundary.
        draft = candidate_from_result(raw_item, result)
        result = replace(result, description=build_candidate_description(draft).replace('Подія: .', 'Назву події потрібно уточнити.'))
    result = validate_result(result)
    if 'event_extraction' in (raw_item.raw_metadata or {}):
        raw_item.raw_metadata['event_extraction']['result'] = result_mapping(result)
    return result


def deferred_poster(raw_item, deterministic):
    reason = 'Suspected multi-event poster without reliable structured extraction; separate associations require moderator review.'
    audit = dict((raw_item.raw_metadata or {}).get('event_extraction', {}))
    audit.update(poster_deferred=True, poster_diagnostics=(raw_item.raw_metadata or {}).get('poster_layout', {}))
    raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'event_extraction': audit}
    return ExtractionBatch(True, unresolved=({'title': None, 'reason': reason,
        'reasons': {'event_group': [reason]}, 'price': None, 'price_status': 'unknown'},), overall_notes=reason)


def candidate_from_result(raw_item, result):
    notes = result.extraction_notes or ''
    if result.address:
        notes = (notes + '\n[Address] extracted: supported extraction evidence.').strip()
    return EventCandidate(
        raw_source_item=raw_item, title=result.title or '', description=result.description or '',
        start_time=result.start_time, end_time=result.end_time, venue_name=result.venue_name or '',
        address=result.address or '', price=result.price, category=result.category or '',
        price_status=infer_price_status(result.price),
        confidence=result.confidence, extraction_notes=notes,
        source_url=raw_item.source_url, source_image_url=raw_item.raw_image_url,
        status=EventCandidate.Status.NEW,
    )


def extract_raw_source_item(raw_item, *, provider=None, dry_run=False, retry_failed=False, reextract=False, resolve_locations=False, force_prices=False):
    from .repair import candidate_complete, repair_existing_candidates
    existing_rows = list(EventCandidate.objects.filter(raw_source_item_id=raw_item.pk).order_by('pk'))
    existing = existing_rows[0] if existing_rows else None
    incomplete = [candidate for candidate in existing_rows if not candidate_complete(candidate)
                  and candidate.status == 'new' and not candidate.event_id]
    if incomplete and reextract:
        return repair_existing_candidates(raw_item, incomplete, provider=provider, dry_run=dry_run)
    # Preserve complete single-event rows. Calendars still support adding distinct
    # entries through the existing explicit batch re-extraction workflow.
    audit = (raw_item.raw_metadata or {}).get('event_extraction', {})
    calendar = len(existing_rows) > 1 or len(raw_item.raw_metadata.get('candidate_identities', {})) > 1
    split_hint = poster_context(raw_item) if reextract and existing else {}
    if existing and (not reextract or not calendar and not force_prices or any(not row.source_fingerprint for row in existing_rows)):
        split_warning = ' Suspected multi-event source: existing identity preserved; compare poster groups and create additional events through moderation.' if split_hint.get('detected_multi_event_poster') else ''
        if split_warning:
            raw_item.raw_metadata = {**raw_item.raw_metadata, 'poster_layout': split_hint}
            if not dry_run:
                with transaction.atomic():
                    current = RawSourceItem.objects.select_for_update().get(pk=raw_item.pk)
                    current.raw_metadata = {**current.raw_metadata, 'poster_layout': split_hint,
                        'poster_split_review': {'reason': split_warning.strip(), 'candidate_ids': [row.pk for row in existing_rows]}}
                    current.save(update_fields=['raw_metadata', 'updated_at'])
        return ExtractionOutcome('skipped', candidate=existing,
            decisions=({'title': existing.title or f'Candidate #{existing.pk}', 'decision': 'existing',
                'target': f'ingestion.EventCandidate #{existing.pk}',
                'reason': ('Existing candidate preserved; use --reextract to retry incomplete fields.' if not candidate_complete(existing) else 'Complete candidate preserved.') + split_warning},))
    eligible = [RawSourceItem.ExtractionStatus.NEW]
    if reextract:
        eligible.append(RawSourceItem.ExtractionStatus.EXTRACTED)
    if retry_failed:
        eligible.append(RawSourceItem.ExtractionStatus.FAILED)
    if raw_item.extraction_status not in eligible:
        return ExtractionOutcome('skipped')
    try:
        prepare_image_text(raw_item)
        result = extract_facts(raw_item, provider=provider)
        if isinstance(result, ExtractionBatch):
            return store_batch(raw_item, result, eligible, dry_run=dry_run, resolve_locations=resolve_locations, force_prices=force_prices)
        # Single-result/deterministic fallbacks use the same readiness and storage
        # decisions as semantic batches. Model fields permit incomplete admin drafts;
        # that does not make them valid extracted candidates.
        multiple = result.is_event and (result.event_count is not None and result.event_count != 1
                                       or 'Multiple or unresolved event count' in ' '.join(result.ambiguities))
        unresolved = ()
        if multiple:
            unresolved = ({**result_mapping(result), 'reason': 'Multiple or unresolved event count; manual selection required.',
                           'reasons': {'event_count': ['Multiple or unresolved event count.']}},)
        elif not result.is_event and result.start_time and result.start_time <= timezone.now():
            unresolved = ({**result_mapping(result), 'reason': 'No upcoming event.',
                           'reasons': {'start_time': ['event start is in the past']}},)
        batch = ExtractionBatch(result.is_event, events=(result,) if result.is_event and not multiple else (),
                                unresolved=unresolved)
        outcome = store_batch(raw_item, batch, eligible, dry_run=dry_run,
                              resolve_locations=resolve_locations, force_prices=force_prices)
        return replace(outcome, result=result)
    except Exception as error:
        # Persist only bounded codes/field messages, never database SQL or provider bodies.
        message = ('Extraction failed validation or deterministic parsing.' if isinstance(error, ExtractionError)
                   else f'Extraction failed ({type(error).__name__}).')
        reasons = {'extraction': [message]}
        if isinstance(error, IntegrityError):
            constraint = getattr(getattr(getattr(error, '__cause__', None), 'diag', None), 'constraint_name', None)
            known = {'unique_raw_candidate_fingerprint': 'source_fingerprint',
                     'unique_source_event_observation': 'source_observation'}
            message = 'Database constraint conflict; retry after inspecting existing source links.'
            reasons = {known.get(constraint, 'database'): [message]}
        failure = {'reason': message, 'reasons': reasons}
        logger.warning('Raw item #%s: %s', raw_item.pk, message)
        if not dry_run:
            with transaction.atomic():
                from django.db import connection
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_xact_lock(%s)', [742_691_001])
                current = RawSourceItem.objects.select_for_update().get(pk=raw_item.pk)
                if not current.candidates.exists() and current.extraction_status in eligible:
                    audit = dict((raw_item.raw_metadata or {}).get('event_extraction', {}))
                    audit.update(persistence_error=failure, candidate_ready=0)
                    current.raw_metadata = {**current.raw_metadata, 'event_extraction': audit}
                    current.extraction_status = RawSourceItem.ExtractionStatus.FAILED
                    current.save(update_fields=['extraction_status', 'raw_metadata', 'updated_at'])
        return ExtractionOutcome('failed', error=message, unresolved=(failure,))


def extract_event_candidate(raw_item, **kwargs):
    """Compatibility boundary: return a private candidate or None; use outcomes for batches."""
    return extract_raw_source_item(raw_item, **kwargs).candidate


def event_identity(result):
    """Use source facts, never array position or later moderator edits."""
    from hashlib import sha256
    from datetime import timezone as utc
    from .services import normalize_fact
    identity = {'title': normalize_fact(result.title),
                'start_time': result.start_time.astimezone(utc.utc).isoformat(),
                'location': normalize_fact(result.venue_name or result.address)}
    import json
    return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), identity


def enrich_missing_location(candidate, event, *, dry_run=False):
    """Fill only unedited blanks on unpublished rows; preserve the source fingerprint."""
    if candidate.status != EventCandidate.Status.NEW or candidate.event_id:
        return False
    from .provenance import moderator_edited_fields
    protected = moderator_edited_fields(candidate)
    # A corrected individual venue must not receive the shared venue's street address.
    from .extractors.location_evidence import location_supported
    conflicting_venue = bool(candidate.venue_name and event.venue_name and
                             not location_supported(candidate.venue_name, event.venue_name))
    updates = {}
    for name in ('venue_name', 'address'):
        if (not getattr(candidate, name) and getattr(event, name) and name not in protected
                and event.location_provenance.get(name) in ('event_specific', 'shared_post_context')
                and not (name == 'address' and conflicting_venue)):
            updates[name] = getattr(event, name)
    if not updates:
        return False
    if not dry_run:
        for name, value in updates.items():
            setattr(candidate, name, value)
        candidate.extraction_notes = (candidate.extraction_notes + '\n[Location facts] Enriched: ' + ', '.join(
            f'{name}={event.location_provenance[name]}' for name in updates)).strip()
        candidate.save(update_fields=[*updates, 'extraction_notes', 'updated_at'])
    return True


def enrich_candidate_pricing(candidate, extracted_price, *, dry_run=False, force=False, missing_status_only=False):
    from .provenance import moderator_edited_fields
    if candidate.status != EventCandidate.Status.NEW or candidate.event_id:
        return False
    if not force and {'price', 'price_status'} & moderator_edited_fields(candidate):
        return False
    if missing_status_only and (candidate.price is not None or candidate.price_status == 'unknown'):
        return False
    status = infer_price_status(extracted_price)
    if candidate.price == extracted_price and candidate.price_status == status:
        return False
    if not dry_run:
        candidate.price, candidate.price_status = extracted_price, status
        candidate.extraction_notes = (candidate.extraction_notes + '\n[Pricing] extracted: ' + status +
            ('; no reliable extracted price.' if extracted_price is None else '; supported price evidence.')).strip()
        candidate.save(update_fields=['price', 'price_status', 'extraction_notes', 'updated_at'])
    return True


def prepare_batch(raw_item, batch, context):
    results, unresolved = [], list(batch.unresolved)
    shared = validated_shared_context(batch.shared_context, context)
    poster_events = []
    if len(context.caption_text) > 8000 or len(context.image_text) > 16000 or any('truncated' in warning.lower() for warning in context.poster_layout.get('warnings', [])):
        reason = 'Bounded source/layout input was truncated; compare the full poster for omitted events.'
        unresolved.append({'title': None, 'reason': reason, 'reasons': {'poster_layout': [reason]}})
    for entry in unresolved:
        if not entry.get('title'):
            continue
        proposal = ExtractionResult(True, title=entry['title'], venue_name=entry.get('venue_name'),
                                    address=entry.get('address'), evidence=entry.get('evidence', {}), event_count=1)
        proposal = scoped_event_evidence(proposal, context) if batch.event_count > 1 else proposal
        proposal = ground_semantic_result(proposal, context, multi_event=True)
        for name in ('venue_name', 'address'):
            individual = proposal.venue_name and proposal.location_provenance.get('venue_name') != 'shared_post_context'
            if not getattr(proposal, name) and shared.get(name) and not (name == 'address' and individual):
                evidence = {**proposal.evidence, name: shared['evidence'][name]}
                proposal = ground_semantic_result(replace(proposal, **{name: shared[name]}, evidence=evidence), context, multi_event=True)
        entry.update(venue_name=proposal.venue_name, address=proposal.address, evidence=proposal.evidence,
                     location_provenance=proposal.location_provenance)
    for event in batch.events:
        try:
            missing_location = {name for name in ('venue_name', 'address') if not getattr(event, name)}
            event = scoped_event_evidence(event, context) if batch.event_count > 1 else event
            event = ground_semantic_result(event, context, multi_event=True)
            hints = event_location_hints(event, context)
            updates, evidence = {}, dict(event.evidence)
            for name in ('venue_name', 'address'):
                selected = [hint for hint in hints if hint['field'] == name]
                if name in missing_location and not getattr(event, name) and len({hint['value'].casefold() for hint in selected}) == 1:
                    updates[name] = selected[0]['value']
                    evidence[name] = [{'source': hint['source'], 'quote': hint['quote']} for hint in selected[:4]]
            if updates:
                event = ground_semantic_result(replace(event, **updates, evidence=evidence), context, multi_event=True)
            # Individual venue overrides post-wide venue AND its associated address.
            individual = event.venue_name and event.location_provenance.get('venue_name') != 'shared_post_context'
            updates, evidence = {}, dict(event.evidence)
            for name in ('venue_name', 'address'):
                if not getattr(event, name) and shared.get(name) and not (name == 'address' and individual):
                    updates[name] = shared[name]
                    evidence[name] = shared['evidence'][name]
            if updates:
                event = ground_semantic_result(replace(event, **updates, evidence=evidence), context, multi_event=True)
            event, group = associate_event(event, context)
            poster_events.append({'title': event.title, 'event_group_index': group['event_group_index'] if group else None,
                'image_index': group['image_index'] if group else None, 'extraction_warning': list(event.ambiguities),
                'ocr_confidence_summary': context.poster_layout.get('ocr_confidence_summary')})
            if event.end_time and event.start_time and event.end_time <= event.start_time:
                event = replace(event, end_time=None, confidence=min(event.confidence, .55),
                                extraction_notes='Ending time is not after start; verify manually.')
            draft = candidate_from_result(raw_item, event)
            event = replace(event, description=build_candidate_description(draft).replace('Подія: .', 'Назву події потрібно уточнити.'))
            results.append(event)
        except Exception:
            unresolved.append({'title': event.title, 'start_time': event.start_time.isoformat() if event.start_time else None,
                               'reason': 'Event evidence/values require manual correction.'})
    # A model can return fewer entries than the visible poster groups. Do not
    # silently mark the source extracted or invent a candidate for an omitted row.
    if context.poster_layout.get('detected_multi_event_poster'):
        from .poster_layout import CLOCK, normalized
        for group in context.poster_layout.get('groups', []):
            if not CLOCK.search(group['text']):
                continue
            scope = normalized(group['text'] + '\n' + group['date_context'])
            covered = any(event.title and normalized(event.title) in scope and
                all(normalized(entry['quote']) in scope for entry in event.evidence.get('start_time', [])) for event in results)
            covered |= any(entry.get('title') and normalized(entry['title']) in scope for entry in unresolved)
            if not covered:
                reason = f"Poster group {group['event_group_index']} was not reliably extracted; moderator must inspect this source section."
                unresolved.append({'title': None, 'reason': reason, 'reasons': {'event_group': [reason]},
                    'event_group_index': group['event_group_index'], 'image_index': group['image_index']})
    batch = replace(batch, events=tuple(results), unresolved=tuple(unresolved), needs_review=True, shared_context=shared)
    audit = dict((raw_item.raw_metadata or {}).get('event_extraction', {}))
    audit.update(batch=batch_mapping(batch), unresolved_schema=list(batch.unresolved),
                 detected=batch.event_count, overall_notes=batch.overall_notes,
                 poster_events=poster_events, poster_diagnostics=context.poster_layout)
    raw_item.raw_metadata = {**(raw_item.raw_metadata or {}), 'event_extraction': audit}
    return batch


def store_batch(raw_item, batch, eligible, *, dry_run, resolve_locations=False, force_prices=False):
    ready, unresolved, duplicates = [], list(batch.unresolved), 0
    decisions = []
    existing = {item.source_fingerprint: item for item in raw_item.candidates.all()} if raw_item.pk else {}
    identities = dict((raw_item.raw_metadata or {}).get('candidate_identities', {}))
    location_suggestions = [*batch.events, *(ExtractionResult(True, title=entry.get('title'),
        venue_name=entry.get('venue_name'), address=entry.get('address'),
        evidence=entry.get('evidence', {}), location_provenance=entry.get('location_provenance', {}))
        for entry in batch.unresolved if entry.get('location_provenance'))]
    seen = set()
    identities_seen = []
    for event in batch.events:
        if not event.title or not event.start_time or event.start_time <= timezone.now():
            reasons={}
            if not event.title: reasons['title']=['Missing or unsupported title.']
            if not event.start_time: reasons['start_time']=['Missing, ambiguous, or unsupported start time.']
            elif event.start_time <= timezone.now(): reasons['start_time']=['event start is in the past']
            unresolved.append({**result_mapping(event),'price_status':infer_price_status(event.price), 'reason': 'Title and valid future start are required.', 'reasons':reasons})
            continue
        fingerprint, identity = event_identity(event)
        if fingerprint in seen or any(previous['title'] == identity['title'] and previous['start_time'] == identity['start_time'] and (not previous['location'] or not identity['location']) for previous in identities_seen):
            duplicates += 1
            continue
        candidate = candidate_from_result(raw_item, event)
        candidate.source_fingerprint = fingerprint
        try:
            candidate.full_clean(validate_constraints=False, exclude=['raw_source_item'] if dry_run and raw_item.pk is None else None)
            decision, _ = persistence_decision(candidate, existing, identities, identity, dry_run=dry_run)
            if decision['decision'] == 'review':
                decisions.append(decision)
                unresolved.append({**result_mapping(event), 'reason': decision['reason'],
                    'reasons': {'duplicate': [decision['reason'] + ' Match: ' + decision['target']]}})
                continue
        except Exception as error:
            unresolved.append({**result_mapping(event), 'reason': 'Candidate fields need manual correction.', 'reasons':getattr(error,'message_dict',{'candidate':['Invalid candidate fields.']})})
            continue
        decisions.append(decision)
        seen.add(fingerprint)
        identities_seen.append(identity)
        ready.append((event, candidate, identity))
    audit = dict(raw_item.raw_metadata.get('event_extraction', {}))
    audit.update(unresolved=unresolved, candidate_ready=len(ready), duplicates=duplicates, decisions=decisions)
    raw_item.raw_metadata['event_extraction'] = audit
    outcome = dict(results=tuple(batch.events), unresolved=tuple(unresolved), detected=batch.event_count,
                   ready=len(ready), duplicates=duplicates, decisions=tuple(decisions))
    if dry_run:
        identities = (raw_item.raw_metadata or {}).get('candidate_identities', {})
        existing = {item.source_fingerprint: item for item in raw_item.candidates.all()} if raw_item.pk else {}
        enriched = pricing_enriched = 0
        for event in location_suggestions:
            if not event.title:
                continue
            fingerprint, identity = enrichment_identity(event, location_suggestions)
            if identity is None:
                continue
            old = match_existing_location_event(existing, identities, fingerprint, identity)
            if old:
                pricing_enriched += int(enrich_candidate_pricing(old, event.price, dry_run=True, force=force_prices))
            if old and enrich_missing_location(old, event, dry_run=True):
                enriched += 1
        return ExtractionOutcome('candidate' if ready else ('review' if batch.is_event else 'ignored'),
            linked=sum(item['decision'] == 'link' for item in decisions),
            matched=sum(item['decision'] in ('existing', 'link') for item in decisions),
            enriched=enriched, pricing_enriched=pricing_enriched, **outcome)
    created, matched, linked, enriched, pricing_enriched = [], 0, 0, 0, 0
    pending_resolution = {}
    with transaction.atomic():
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [742_691_001])
        current = RawSourceItem.objects.select_for_update().get(pk=raw_item.pk)
        if current.extraction_status not in eligible:
            outcome.update(ready=0, decisions=({'decision': 'skip', 'title': None,
                'reason': 'Raw extraction status changed during processing; no writes made.'},))
            return ExtractionOutcome('skipped', **outcome)
        existing = {item.source_fingerprint: item for item in current.candidates.select_for_update().order_by('pk')}
        if '' in existing:
            outcome.update(ready=0, decisions=({'decision': 'existing', 'title': None,
                'target': f'ingestion.EventCandidate #{existing[""].pk}',
                'reason': 'Existing legacy candidate appeared during processing; preserved.'},))
            return ExtractionOutcome('skipped', candidate=existing[''], **outcome)
        identities = dict((current.raw_metadata or {}).get('candidate_identities', {}))
        # Existing rows can gain location evidence even when an event is now past.
        # Future-date checks still control creation of new suggestions.
        for event in location_suggestions:
            if not event.title:
                continue
            fingerprint, identity = enrichment_identity(event, location_suggestions)
            if identity is None:
                continue
            old = match_existing_location_event(existing, identities, fingerprint, identity)
            if old:
                pricing_enriched += int(enrich_candidate_pricing(old, event.price, force=force_prices))
            if old and enrich_missing_location(old, event):
                enriched += 1
                pending_resolution[old.pk] = old
        actual_decisions = [entry for entry in decisions if entry['decision'] == 'review']
        accepted = 0
        for event, candidate, identity in ready:
            fingerprint = candidate.source_fingerprint
            try:
                decision, old = persistence_decision(candidate, existing, identities, identity, dry_run=False)
            except Exception as error:
                unresolved.append({**result_mapping(event), 'reason': 'Candidate fields need manual correction.',
                    'reasons': getattr(error, 'message_dict', {'candidate': ['Persistence validation failed.']})})
                continue
            actual_decisions.append(decision)
            if decision['decision'] == 'review':
                unresolved.append({**result_mapping(event), 'reason': decision['reason'],
                    'reasons': {'duplicate': [decision['reason'] + ' Match: ' + decision['target']]}})
                continue
            accepted += 1
            if decision['decision'] == 'existing':
                matched += 1
                continue  # Never overwrite facts, review state, or moderator corrections.
            from .deduplication import store_observation
            candidate, added = store_observation(candidate)
            if not added:
                matched += 1
                linked += 1
                continue
            existing[fingerprint] = candidate
            identities[fingerprint] = identity
            decision['target'] = f'ingestion.EventCandidate #{candidate.pk}'
            created.append(candidate)
            pending_resolution[candidate.pk] = candidate
        outcome.update(ready=accepted, unresolved=tuple(unresolved), decisions=tuple(actual_decisions))
        raw_item.raw_metadata['event_extraction'].update(candidate_ready=accepted, unresolved=unresolved,
                                                        decisions=actual_decisions)
        metadata = {**(current.raw_metadata or {}), **{key: value for key, value in raw_item.raw_metadata.items()
                    if key.startswith('image_text') or key in ('event_extraction', 'poster_layout')}, 'candidate_identities': identities}
        current.raw_metadata = metadata
        current.extraction_status = (RawSourceItem.ExtractionStatus.NEW if unresolved or not accepted and batch.is_event
            else RawSourceItem.ExtractionStatus.EXTRACTED if accepted else RawSourceItem.ExtractionStatus.IGNORED)
        current.save(update_fields=['raw_metadata', 'extraction_status', 'updated_at'])
    if resolve_locations:
        from .geocoding import NominatimProvider, enrich_candidate_location
        geocoder = NominatimProvider(wait_for_slot=True)
        for candidate in pending_resolution.values():
            enrich_candidate_location(candidate, provider=geocoder)
    return ExtractionOutcome('created' if created else 'skipped' if matched else 'review' if batch.is_event else 'ignored',
                             candidate=created[0] if created else None, candidates=tuple(created), matched=matched, linked=linked, enriched=enriched, pricing_enriched=pricing_enriched, **outcome)


def match_existing_location_event(existing, identities, fingerprint, identity):
    old = existing.get(fingerprint)
    if old is not None:
        return old
    matches = [existing[key] for key, previous in identities.items() if key in existing
        and previous['title'] == identity['title'] and (identity['start_time'] is None or previous['start_time'] == identity['start_time'])
        and (not previous['location'] or not identity['location'])]
    return matches[0] if len(matches) == 1 else None


def enrichment_identity(event, events):
    if event.start_time:
        return event_identity(event)
    from .services import normalize_fact
    title = normalize_fact(event.title)
    if sum(normalize_fact(other.title) == title for other in events) != 1:
        return None, None
    # This only fills location blanks on a uniquely matched existing source entry;
    # it neither changes dates nor creates a new candidate with an unresolved time.
    return None, {'title': title, 'start_time': None, 'location': normalize_fact(event.venue_name or event.address)}
