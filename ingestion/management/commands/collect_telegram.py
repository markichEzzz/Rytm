from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from ingestion.models import SourceAccount
from ingestion.providers.telegram import TelegramCollector, TelegramCollectionError
from ingestion.services import store_collected_item


class Command(BaseCommand):
    help = 'Collect bounded public Telegram observations into the shared private ingestion pipeline.'

    def add_arguments(self, parser):
        parser.add_argument('--username')
        parser.add_argument('--limit', type=int, default=5)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 20:
            raise CommandError('--limit must be between 1 and 20.')
        sources = SourceAccount.objects.filter(platform='telegram', enabled=True)
        if options['username']:
            username = options['username'].strip().lstrip('@').lower()
            sources = sources.filter(username__iexact=username)
            if not sources.exists():
                # Initial channels can be previewed without seeding/mutating the live DB.
                if options['dry_run'] and not SourceAccount.objects.filter(platform='telegram',username__iexact=username).exists():
                    sources = [SourceAccount(platform='telegram',username=username)]
                else:
                    raise CommandError('Username must identify an enabled Telegram source.')
        collector = TelegramCollector(limit=options['limit'])
        attempted = successful = failed = created = no_new = 0
        for source in sources:
            attempted += 1
            try:
                items = collector.fetch_items(source)
                existing = set(source.raw_items.values_list('external_id',flat=True)) if source.pk else set()
                new_count = sum(item.external_id not in existing for item in items)
                no_new += int(new_count == 0)
                if not options['dry_run']:
                    with transaction.atomic():
                        current = SourceAccount.objects.select_for_update().get(pk=source.pk,enabled=True,platform='telegram')
                        for item in items:
                            _, added = store_collected_item(current,item)
                            created += int(added)
                        current.last_checked_at = timezone.now()
                        current.save(update_fields=['last_checked_at','updated_at'])
                successful += 1
                latest = max((item.published_at for item in items if item.published_at), default=None)
                self.stdout.write(f'@{source.username}: {"working" if items else "empty/no posts"}; fetched={len(items)}; new={new_count}; latest={latest}; dry-run={options["dry_run"]}')
            except Exception as error:
                failed += 1
                message = str(error) if isinstance(error,TelegramCollectionError) else 'collection_failed'
                self.stdout.write(f'@{source.username}: {message}')
        collector.close()
        self.stdout.write(f'Sources attempted: {attempted}\nSuccessful: {successful}\nNo new posts: {no_new}\nFailed: {failed}\nRaw items created: {created}')
        if failed:
            raise CommandError('Telegram collection finished with source failures.')
