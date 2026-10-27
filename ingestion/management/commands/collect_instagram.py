import logging
import time

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ingestion.models import SourceAccount
from ingestion.providers.instagram import DEFAULT_LIMIT, MAX_LIMIT, InstagramCollector, InstagramCollectionError
from ingestion.services import store_collected_item

logger = logging.getLogger(__name__)
ACCOUNT_DELAY_SECONDS = 2


class Command(BaseCommand):
    help = 'Collect a bounded window of public Instagram post metadata into private raw items only.'

    def add_arguments(self, parser):
        parser.add_argument('--username', help='An enabled configured Instagram source username.')
        parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help=f'Recent posts per account (1–{MAX_LIMIT}).')
        parser.add_argument('--debug-browser', action='store_true', help='Print sanitized Playwright DOM diagnostics; no snapshots or session data.')
        parser.add_argument('--dry-run', action='store_true', help='Fetch summaries without changing the database.')

    def handle(self, *args, **options):
        limit = options['limit']
        if not 1 <= limit <= MAX_LIMIT:
            raise CommandError(f'--limit must be between 1 and {MAX_LIMIT}.')
        sources = SourceAccount.objects.filter(platform=SourceAccount.Platform.INSTAGRAM, enabled=True)
        if options['username']:
            sources = sources.filter(username__iexact=options['username'].strip().lstrip('@'))
            if not sources.exists():
                raise CommandError('Username must identify an enabled configured Instagram source.')
        sources = list(sources.order_by('pk'))
        processed = fetched = created = existing = successful = no_new = 0
        failures = []
        try:
            collector = InstagramCollector(limit=limit)
            if options['debug_browser']:
                if not hasattr(collector.provider, 'set_diagnostics'):
                    raise InstagramCollectionError('--debug-browser requires INSTAGRAM_COLLECTION_PROVIDER=playwright.')
                collector.provider.set_diagnostics(self.stdout.write)
        except InstagramCollectionError as error:
            raise CommandError(str(error)) from None
        try:
            for index, source in enumerate(sources):
                if index:
                    time.sleep(ACCOUNT_DELAY_SECONDS)
                processed += 1
                attempted_at = timezone.now()
                try:
                    items = collector.fetch_items(source)
                    if len(items) > limit:
                        raise InstagramCollectionError('Provider returned more items than the requested limit.')
                    fetched += len(items)
                    ids = set(source.raw_items.values_list("external_id", flat=True))
                    no_new += int(not any(item.external_id not in ids for item in items))
                    if options['dry_run']:
                        successful += 1
                        self.stdout.write(f'@{source.username}: {len(items)} recent items fetched (dry-run).')
                        continue
                    new_count = existing_count = 0
                    # One source is all-or-nothing. Never hold a transaction during network access.
                    with transaction.atomic():
                        current = SourceAccount.objects.select_for_update().get(pk=source.pk)
                        if not current.enabled or current.platform != SourceAccount.Platform.INSTAGRAM:
                            raise InstagramCollectionError('Source was disabled or changed during collection.')
                        for item in items:
                            _, added = store_collected_item(current, item)
                            new_count += int(added)
                            existing_count += int(not added)
                        current.last_checked_at = attempted_at
                        current.save(update_fields=['last_checked_at', 'updated_at'])
                    successful += 1
                    created += new_count
                    existing += existing_count
                    self.stdout.write(f'@{source.username}: {len(items)} fetched, {new_count} new, {existing_count} existing.')
                except Exception as error:
                    if isinstance(error, InstagramCollectionError):
                        message = str(error)
                    elif isinstance(error, ValidationError):
                        message = 'Collected item failed validation; source transaction was rolled back.'
                    else:
                        message = f'Collection failed ({type(error).__name__}); source changes were rolled back.'
                    # Deliberately exclude original exception strings/tracebacks: they may contain secrets.
                    failures.append((source.username, message))
                    logger.warning('Instagram collection failed for source #%s: %s', source.pk, message)
        finally:
            collector.close()
        self.stdout.write(
            f'Processed sources: {processed}\nFetched items: {fetched}\nNew RawSourceItems: {created}\n'
            f'Existing items skipped: {existing}\nFailures: {len(failures)}\n'
            f'Sources attempted: {processed}\nSuccessful: {successful}\nNo new posts: {no_new}\nFailed: {len(failures)}\nRaw items created: {created}'
        )
        for username, message in failures:
            self.stderr.write(f'@{username}: {message}')
        if failures:
            raise CommandError('Instagram collection finished with failures; see the source summaries above.')
