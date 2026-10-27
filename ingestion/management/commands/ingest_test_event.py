from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ingestion.collectors import CollectedSourceItem
from ingestion.models import SourceAccount, EventCandidate, RawSourceItem
from ingestion.services import store_collected_item


class Command(BaseCommand):
    help = 'Create one synthetic internal raw item/candidate, without network access or publication.'

    def add_arguments(self, parser):
        parser.add_argument('--username', default='festrepublic')
        parser.add_argument('--external-id', default='rytm-test-event')

    def handle(self, *args, **options):
        username = options['username'].strip().lower().lstrip('@')
        account = SourceAccount.objects.filter(platform='instagram', username=username, enabled=True).first()
        if account is None:
            raise CommandError('An enabled configured source is required. Run seed_source_accounts or add one in admin.')
        external_id = options['external_id']
        with transaction.atomic():
            raw, _ = store_collected_item(account, CollectedSourceItem(
                external_id=external_id, source_url='https://example.com/rytm-offline-test',
                raw_text='SYNTHETIC INTERNAL SOURCE TEXT — not public copy.',
                raw_metadata={'synthetic': True, 'origin': 'ingest_test_event'}, published_at=timezone.now(),
            ))
            if not raw.raw_metadata.get('synthetic'):
                raise CommandError('That external ID belongs to a real source item; choose another test ID.')
            candidate, created = EventCandidate.objects.get_or_create(raw_source_item=raw, defaults={
                'title': 'Rytm offline test event',
                'start_time': timezone.now() + timedelta(days=7),
                'end_time': timezone.now() + timedelta(days=7, hours=2),
                'venue_name': 'Test venue', 'address': 'Lviv, test address',
                'latitude': 49.8397, 'longitude': 24.0297, 'price': 0, 'category': 'party',
                'source_url': raw.source_url, 'confidence': 1,
                'extraction_notes': 'Synthetic facts supplied by the command, not extracted from a source.',
            })
            if created:
                raw.extraction_status = RawSourceItem.ExtractionStatus.EXTRACTED
                raw.save(update_fields=['extraction_status', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'Candidate #{candidate.pk} ({candidate.status}). No Event was published. '
            'Review in admin and upload your own public photo before approval.'
        ))
