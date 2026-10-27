from dataclasses import asdict
import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from ingestion.extraction import configured_provider, extract_raw_source_item
from ingestion.models import RawSourceItem


class Command(BaseCommand):
    help = 'Extract private event candidates from raw source items; never approve or publish.'

    def add_arguments(self, parser):
        parser.add_argument('--id', type=int, dest='item_id')
        parser.add_argument('--source', help='Configured source username, optional leading @.')
        parser.add_argument('--platform', choices=['instagram','telegram'])
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--retry-failed', action='store_true')
        parser.add_argument('--reextract', action='store_true', help='Add new fingerprinted events without overwriting reviewed candidates.')
        parser.add_argument('--debug-json', action='store_true', help='Show validated extraction diagnostics as JSON.')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--resolve-locations', action='store_true', help='Use the existing location resolver after enrichment; previews do not persist.')
        parser.add_argument('--force-prices', action='store_true', help='Explicitly replace moderator pricing with validated re-extraction pricing.')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 100:
            raise CommandError('--limit must be between 1 and 100.')
        if options['item_id'] is not None and options['item_id'] < 1:
            raise CommandError('--id must be positive.')
        statuses = ['new'] + (['extracted'] if options['reextract'] else []) + (['failed'] if options['retry_failed'] else [])
        items = RawSourceItem.objects.select_related('source_account').filter(extraction_status__in=statuses)
        if options['platform']:
            items = items.filter(source_account__platform=options['platform'])
        if options['item_id'] is not None:
            if not RawSourceItem.objects.filter(pk=options['item_id']).exists():
                raise CommandError('Raw source item does not exist.')
            items = items.filter(pk=options['item_id'])
        if options['source']:
            items = items.filter(source_account__username__iexact=options['source'].strip().lstrip('@'))
        provider = configured_provider()
        if not (options['dry_run'] and options['debug_json']):
            self.stdout.write(f'Provider: {getattr(provider, "name", "deterministic")} | Model: {getattr(provider, "model", None) or "—"}')
        counts = dict(processed=0, created=0, ignored=0, failed=0, skipped=0, candidate=0, review=0, enriched=0, pricing=0, linked=0, reused=0, manual_events=0, repair=0, repair_failed=0)
        for item in items.order_by('pk')[:options['limit']]:
            outcome = extract_raw_source_item(item, provider=provider, dry_run=options['dry_run'],
                                              retry_failed=options['retry_failed'], reextract=options['reextract'], resolve_locations=options['resolve_locations'], force_prices=options['force_prices'])
            counts['processed'] += 1
            counts['enriched'] += outcome.enriched
            counts['pricing'] += outcome.pricing_enriched
            counts['linked'] += outcome.linked
            counts['reused'] += max(0, outcome.matched - outcome.linked)
            counts['manual_events'] += len(outcome.unresolved)
            if outcome.action != 'skipped' or not outcome.linked:
                amount = outcome.repaired if outcome.action == 'repair' else len(outcome.candidates) or 1 if outcome.action == 'created' else outcome.ready or 1 if outcome.action == 'candidate' else 1
                counts[outcome.action] += amount
            if options['dry_run'] and options['debug_json']:
                self.stdout.write(json.dumps({
                    'raw_item_id': item.pk, 'source': '@' + item.source_account.username, 'action': outcome.action,
                    'provider': (item.raw_metadata or {}).get('event_extraction', {}).get('provider', 'deterministic'),
                    'model': (item.raw_metadata or {}).get('event_extraction', {}).get('model'),
                    'caption_found': bool(item.raw_text),
                    'image_text_found': bool((item.raw_metadata or {}).get('image_text')),
                    'image_text_status': (item.raw_metadata or {}).get('image_text_status', 'no_images'),
                    'result': asdict(outcome.result) if outcome.result else None,
                    'deterministic_result': (item.raw_metadata or {}).get('event_extraction', {}).get('deterministic_result'),
                    'provenance': {name: sorted({entry['source'] for entry in entries}) for name, entries in outcome.result.evidence.items()} if outcome.result else {},
                    'events': [asdict(event) for event in outcome.results],
                    'detected': outcome.detected, 'candidate_ready': outcome.ready,
                    'poster_diagnostics': (item.raw_metadata or {}).get('poster_layout', {}),
                    'poster_events': (item.raw_metadata or {}).get('event_extraction', {}).get('poster_events', []),
                    'unresolved': outcome.unresolved, 'duplicates': outcome.duplicates,
                    'error': outcome.error, 'decisions': outcome.decisions,
                    'duplicates_linked': outcome.linked, 'existing_reused': max(0, outcome.matched - outcome.linked),
                    'repairs': outcome.repairs, 'candidates_repaired': outcome.repaired,
                    'fallback_reason': (item.raw_metadata or {}).get('event_extraction', {}).get('fallback_reason'),
                }, cls=DjangoJSONEncoder, ensure_ascii=False))
            elif options['dry_run']:
                self.print_preview(item, outcome)
            else:
                self.print_decisions(item, outcome, dry_run=False)
            if options['dry_run'] and options['resolve_locations']:
                from ingestion.extraction import candidate_from_result
                from ingestion.geocoding import NominatimProvider, enrich_candidate_location
                geocoder = NominatimProvider(wait_for_slot=True, persist_cache=False)
                for event in outcome.results or ((outcome.result,) if outcome.result else ()):
                    preview = candidate_from_result(item, event)
                    resolution = enrich_candidate_location(preview, provider=geocoder, dry_run=True)
                    self.stdout.write(f'Location preview: {event.title or "unknown"}: {resolution.status} — {resolution.message}')
        self.stdout.write(
            f"Processed: {counts['processed']}\nCandidates created: {counts['created']}\n"
            f"Candidates repaired{' (preview)' if options['dry_run'] else ''}: {counts['repair']}\n"
            f"Candidate repairs needing review: {counts['repair_failed']}\n"
            f"Manual review without candidate: {counts['review']}\n"
            f"Duplicates linked{' (preview)' if options['dry_run'] else ''}: {counts['linked']}\n"
            f"Existing candidates reused{' (preview)' if options['dry_run'] else ''}: {counts['reused']}\n"
            f"Manual review events: {counts['manual_events']}\nIgnored: {counts['ignored']}\nFailed: {counts['failed']}\nSkipped: {counts['skipped']}"
        )
        if options['dry_run']:
            self.stdout.write(f"Dry-run candidates (not saved): {counts['candidate']}")
        self.stdout.write(f"Location fields enriched{' (preview)' if options['dry_run'] else ''}: {counts['enriched']}")
        self.stdout.write(f"Pricing normalized{' (preview)' if options['dry_run'] else ''}: {counts['pricing']}")
        if counts['failed']:
            raise CommandError('Extraction finished with failures; see sanitized log messages.')

    def print_preview(self, item, outcome):
        audit = (item.raw_metadata or {}).get('event_extraction', {})
        events = outcome.results or ((outcome.result,) if outcome.result else ())
        self.stdout.write(f'Raw item #{item.pk} — {item.source_account.platform} @{item.source_account.username}')
        self.stdout.write(f"Provider: {audit.get('retry_provider') or audit.get('attempted_provider') or audit.get('provider', 'deterministic')} | Model: {audit.get('retry_model') or audit.get('attempted_model') or audit.get('model') or '—'}")
        if audit.get('provider_response_seconds') is not None:
            self.stdout.write(f"Model response time: {audit['provider_response_seconds']:.3f}s")
        if outcome.action in ('repair', 'repair_failed'):
            self.stdout.write('Provider retried: ' + ('yes' if audit.get('provider_retried') else 'current deterministic extractor'))
        self.stdout.write(f"Caption: {'yes' if item.raw_text else 'no'} | OCR: {'yes' if (item.raw_metadata or {}).get('image_text') else 'no'}")
        layout = (item.raw_metadata or {}).get('poster_layout', {})
        self.stdout.write(f"OCR blocks: {layout.get('ocr_block_count', 0)} | Suspected multi-event: {bool(layout.get('detected_multi_event_poster'))} | Logical groups: {len(layout.get('groups', []))}")
        for warning in layout.get('warnings', []):
            self.stdout.write('Poster review: ' + warning)
        self.stdout.write(f'Detected events: {outcome.detected if outcome.results or outcome.unresolved else int(bool(outcome.result and outcome.result.is_event))}')
        for index, event in enumerate(events, 1):
            self.stdout.write(f'\n[{index}] Title: {event.title or "unresolved"}')
            self.stdout.write(f'Start: {event.start_time.isoformat() if event.start_time else "unresolved"}')
            from events.pricing import infer_price_status
            self.stdout.write(f'Price status: {infer_price_status(event.price)}')
            from ingestion.extraction import candidate_from_result
            from ingestion.deduplication import match_diagnostics
            self.stdout.write('Dedup: '+json.dumps(match_diagnostics(candidate_from_result(item,event)),ensure_ascii=False))
            self.stdout.write(f'End: {event.end_time.isoformat() if event.end_time else "—"}')
            self.stdout.write(f'Venue: {event.venue_name or "unresolved"} | Price: {event.price if event.price is not None else "unknown"}')
            self.stdout.write(f'Address: {event.address or "-"} | Venue provenance: {event.location_provenance.get("venue_name", "unresolved")}')
            self.stdout.write(f'Location evidence: {"usable for resolver" if event.venue_name or event.address else "missing"}')
            self.stdout.write(f'Category: {event.category or "unknown"} | Confidence: {event.confidence:.2f} | Manual review: yes')
            if event.ambiguities:
                self.stdout.write('Review: ' + '; '.join(event.ambiguities))
        for entry in outcome.unresolved:
            self.stdout.write(f'Unresolved: {entry.get("title") or ('Candidate #' + str(entry['candidate_id']) if entry.get('candidate_id') else 'unknown title')} — {entry.get("reason", "manual completion required")}')
            for field,messages in entry.get('reasons',{}).items():
                for message in messages:
                    self.stdout.write(f'  {field}: {message}')
            self.stdout.write(f'Start: {entry.get("start_time") or "unresolved"} | Category: {entry.get("category") or "unknown"} | Price: {entry.get("price")} | Price status: {entry.get("price_status", "unknown")}')
            if entry.get('location_provenance'):
                self.stdout.write(f'Venue: {entry.get("venue_name") or "-"} | Address: {entry.get("address") or "-"} | Venue provenance: {entry["location_provenance"].get("venue_name", "unresolved")}')
        self.print_decisions(item, outcome, dry_run=True)
        ready = outcome.ready if outcome.results or outcome.unresolved else int(outcome.action == 'candidate')
        self.stdout.write(f'\nSummary: Extracted: {outcome.detected if outcome.results or outcome.unresolved else len(events)} | Candidate-ready: {ready} | Needs manual completion: {len(outcome.unresolved)} | Duplicates: {outcome.duplicates} | Persisted: 0 (dry-run)')

    def print_decisions(self, item, outcome, *, dry_run):
        if not (outcome.decisions or outcome.unresolved or outcome.error):
            return
        if not dry_run:
            self.stdout.write(f'Raw item #{item.pk} — {item.source_account.platform} @{item.source_account.username}')
        audit = (item.raw_metadata or {}).get('event_extraction', {})
        if audit.get('retry_failed'):
            self.stdout.write('Provider retry failed: ' + audit.get('fallback_reason', 'provider_unavailable') +
                (f" (HTTP {audit['provider_http_status']})" if audit.get('provider_http_status') else ''))
        elif audit.get('status') == 'fallback':
            self.stdout.write('Provider fallback: ' + audit.get('fallback_reason', 'reason not recorded in historical audit') +
                (f" (HTTP {audit['provider_http_status']})" if audit.get('provider_http_status') else ''))
        for decision in outcome.decisions:
            if decision['decision'] == 'review':
                continue  # Field reasons below include the duplicate target.
            verb = {'create': 'Would create' if dry_run else 'Created',
                    'link': 'Would link' if dry_run else 'Linked', 'existing': 'Preserved existing', 'skip': 'Skipped', 'repair': 'Would repair' if dry_run else 'Repaired'}[decision['decision']]
            target = ' — ' + decision['target'] if decision.get('target') else ''
            self.stdout.write(f'{decision.get("title") or "Unknown title"}: {verb}{target}. {decision["reason"]}')
        for repair in outcome.repairs:
            self.stdout.write(f'Candidate #{repair["candidate_id"]} {"repair preview" if dry_run else "repaired"}:')
            for field, change in repair['changes'].items():
                self.stdout.write(f'  {field}: {change["before"] if change["before"] not in (None, "") else "[blank]"} -> {change["after"]}')
            self.stdout.write('Candidate status: ' + ('would become complete' if dry_run else 'complete'))
        if not dry_run:
            for entry in outcome.unresolved:
                self.stdout.write(f'{entry.get("title") or ('Candidate #' + str(entry['candidate_id']) if entry.get('candidate_id') else 'Unknown title')}: Not persisted — {entry.get("reason", "Manual completion required.")}')
                for field, messages in entry.get('reasons', {}).items():
                    for message in messages:
                        self.stdout.write(f'  {field}: {message}')
        if outcome.error:
            self.stdout.write('Not persisted: ' + outcome.error)
