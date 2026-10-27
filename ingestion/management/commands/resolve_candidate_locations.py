from django.core.management.base import BaseCommand, CommandError

from ingestion.geocoding import NominatimProvider, enrich_candidate_location
from ingestion.models import EventCandidate


class Command(BaseCommand):
    help = 'Resolve locations for existing unpublished candidates; never approve or publish.'

    def add_arguments(self, parser):
        parser.add_argument('--source', help='Source username, optional leading @.')
        parser.add_argument('--limit', type=int, help='Maximum candidates to inspect; default is all matching candidates.')
        parser.add_argument('--id', type=int, dest='candidate_id')
        parser.add_argument('--dry-run', action='store_true', help='Resolve without changing candidates or lookup cache.')
        parser.add_argument('--force', action='store_true', help='Replace existing coordinates on successful resolution, including manual coordinates.')

    def handle(self, *args, **options):
        if options['limit'] is not None and options['limit'] < 1:
            raise CommandError('--limit must be positive.')
        candidate_id = options['candidate_id']
        if candidate_id is not None:
            if candidate_id < 1:
                raise CommandError('--id must be positive.')
            if not EventCandidate.objects.filter(pk=candidate_id).exists():
                raise CommandError('Event candidate does not exist.')
        candidates = EventCandidate.objects.filter(status=EventCandidate.Status.NEW, event__isnull=True)
        if candidate_id is not None:
            candidates = candidates.filter(pk=candidate_id)
            if not candidates.exists():
                raise CommandError('Candidate is not unpublished and awaiting review.')
        if options['source']:
            candidates = candidates.filter(raw_source_item__source_account__username__iexact=options['source'].strip().lstrip('@'))
        provider = NominatimProvider(wait_for_slot=True, persist_cache=not options['dry_run'], require_address=True)
        counts = dict(processed=0, resolved=0, already_coordinates=0, no_evidence=0, ambiguous=0, failed=0)
        coordinates = addresses = complete = skipped = 0
        if options['dry_run']:
            self.stdout.write('Dry run: no database changes; external lookups may be performed.')
        candidates = candidates.order_by('pk')
        if options['limit'] is not None:
            candidates = candidates[:options['limit']]
        for candidate in candidates.iterator(chunk_size=100):
            outcome = enrich_candidate_location(candidate, provider=provider, dry_run=options['dry_run'], force=options['force'], enrich_addresses=True)
            counts['processed'] += 1
            counts[outcome.status if outcome.status in counts else 'failed'] += 1
            if candidate.latitude is not None and candidate.longitude is not None and outcome.status != 'already_coordinates' and not outcome.coordinates_enriched:
                counts['already_coordinates'] += 1
            coordinates += int(outcome.coordinates_enriched)
            addresses += int(outcome.address_enriched)
            complete += int(outcome.already_complete)
            skipped += int(outcome.status in ('no_evidence', 'already_coordinates', 'ineligible'))
            self.stdout.write(f'\nCandidate #{candidate.pk}\nTitle: {candidate.title}\nVenue: {candidate.venue_name or "-"}\nExisting address: {candidate.address or "-"}\nResolved address: {outcome.address or "-"}')
            self.stdout.write(f'Result: {outcome.status}\nReason: {outcome.message}')
            if outcome.latitude is not None or outcome.longitude is not None:
                self.stdout.write(f'Latitude: {outcome.latitude}\nLongitude: {outcome.longitude}\nSource: {outcome.source}')
            self.stdout.write(f'Address source: {outcome.address_source or "-"}')
        self.stdout.write(
            f'\nProcessed: {counts["processed"]}\nResolved: {counts["resolved"]}\n'
            f'Already had coordinates: {counts["already_coordinates"]}\nSkipped no evidence: {counts["no_evidence"]}\n'
            f'Ambiguous: {counts["ambiguous"]}\nFailed: {counts["failed"]}'
        )
        self.stdout.write(f'Coordinates enriched: {coordinates}\nAddresses enriched: {addresses}\nAlready complete: {complete}\nSkipped: {skipped}')
