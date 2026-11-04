import logging
import json
import time
from datetime import datetime, timezone as datetime_timezone

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ingestion.models import SourceAccount
from ingestion.instagram_policy import InstagramPolicy
from ingestion.instagram_schedule import InstagramSchedule
from ingestion.providers.instagram import (MAX_LIMIT, InstagramCollector, InstagramCollectionError,
                                          selected_provider, configured_provider_name)
from ingestion.providers.instagram_safety import MESSAGES
from ingestion.services import store_collected_item

logger = logging.getLogger(__name__)


def timestamp(value):
    return datetime.fromtimestamp(value, datetime_timezone.utc).isoformat()


class Command(BaseCommand):
    help = 'Collect a small due-only window of public Instagram metadata; stop on access restrictions.'

    def add_arguments(self, parser):
        parser.add_argument('--username', '--source', dest='username', help='Enabled configured Instagram username (still honors scheduling/cooldowns).')
        parser.add_argument('--provider', choices=['playwright', 'instaloader'], help='Explicit provider for this run; no automatic fallback.')
        parser.add_argument('--source-id', type=int, help='Enabled configured Instagram source PK.')
        parser.add_argument('--limit', '--limit-posts', dest='limit', type=int, default=None, help=f'Newest links scanned per source (1–{MAX_LIMIT}); --limit is the compatibility alias.')
        parser.add_argument('--limit-sources', type=int, help='Maximum due sources attempted (1–100).')
        parser.add_argument('--debug-browser', action='store_true', help='Print sanitized counts/flags, never page content or session data.')
        parser.add_argument('--dry-run', action='store_true', help='Fetch metadata without raw/source DB writes; safety scheduling still persists.')
        parser.add_argument('--plan', action='store_true', help='Show due selection without browser/Instagram requests or schedule updates.')

    @staticmethod
    def _store(source, items, attempted_at, completed):
        created = existing = 0
        with transaction.atomic():
            current = SourceAccount.objects.select_for_update().get(pk=source.pk)
            if not current.enabled or current.platform != SourceAccount.Platform.INSTAGRAM:
                raise InstagramCollectionError('Source was disabled or changed during collection.', reason='source_changed')
            for item in items:
                _, added = store_collected_item(current, item)
                created += int(added)
                existing += int(not added)
            if completed:
                current.last_checked_at = attempted_at
                current.save(update_fields=['last_checked_at', 'updated_at'])
        return created, existing

    def handle(self, *args, **options):
        counts = dict(attempted=0, fetched=0, created=0, existing=0, successful=0, no_new=0, failed=0)
        collector = None
        self.result_status = 'not_started'
        try:
            policy = InstagramPolicy.load()
            provider_name = options['provider'] or configured_provider_name()
            self.stdout.write(f'Instagram provider: {provider_name}' +
                              (' (uses the same persistent profile as instagram_browser_login).' if provider_name == 'playwright'
                               else ' (does not use the instagram_browser_login browser session).'))
            if provider_name == 'playwright' and (options['debug_browser'] or options['verbosity'] >= 2):
                from ingestion.providers.instagram_browser import profile_configuration
                self.stdout.write('Collection browser: ' + json.dumps(profile_configuration()))
            limit = options['limit'] if options['limit'] is not None else policy.max_posts
            source_limit = options['limit_sources'] if options['limit_sources'] is not None else policy.max_sources
            if not 1 <= limit <= MAX_LIMIT or not 1 <= source_limit <= 100:
                raise InstagramCollectionError(f'Post limit must be 1–{MAX_LIMIT}; source limit must be 1–100.', reason='configuration')
            if options['source_id'] is not None and options['source_id'] < 1:
                raise InstagramCollectionError('--source-id must be a positive configured source PK.', reason='configuration')
            query = SourceAccount.objects.filter(platform=SourceAccount.Platform.INSTAGRAM, enabled=True)
            if options['username']:
                query = query.filter(username__iexact=options['username'].strip().lstrip('@'))
            if options['source_id'] is not None:
                query = query.filter(pk=options['source_id'])
            sources = list(query)
            if (options['username'] or options['source_id'] is not None) and not sources:
                raise InstagramCollectionError('Source selector must identify an enabled configured Instagram source.', reason='configuration')
            with InstagramSchedule(policy) as schedule:
                now = timezone.now().timestamp()
                due = sorted((source for source in sources if schedule.due_at(source) <= now), key=lambda source: (schedule.due_at(source), source.pk))
                chosen = due[:source_limit]
                self.stdout.write(f'Configured sources: {len(sources)}; due: {len(due)}; selected: {len(chosen)}; deferred: {len(sources)-len(chosen)}; post limit: {limit}.')
                if schedule.data['global_until'] > now:
                    reason = schedule.data['global_reason'] if schedule.data['global_reason'] in MESSAGES else 'access_blocked'
                    self.stdout.write(f'Instagram status: cooldown; reason={reason}; until={timestamp(schedule.data["global_until"])}. No requests made.')
                    if not options['plan']:
                        raise InstagramCollectionError('Instagram global safety cooldown is active; no requests made.', reason='cooldown')
                if options['plan']:
                    for source in chosen:
                        self.stdout.write(f'Due source #{source.pk}: @{source.username}')
                    for source in sources:
                        if source not in due:
                            self.stdout.write(f'Deferred source #{source.pk}: @{source.username}; due={timestamp(schedule.due_at(source))}.')
                    self.result_status = 'plan'
                    return
                if not chosen:
                    self.result_status = 'no_due_sources'
                    return
                if schedule.data['pacing_until'] > now:
                    self.stdout.write(f'Instagram status: pacing; next run not before {timestamp(schedule.data["pacing_until"])}. No requests made.')
                    self.result_status = 'pacing'
                    return
                collector = (InstagramCollector(limit=limit, provider=selected_provider(provider_name)) if options['provider']
                             else InstagramCollector(limit=limit))
                if options['debug_browser'] or options['verbosity'] >= 2 and provider_name == 'playwright':
                    if not hasattr(collector.provider, 'set_diagnostics'):
                        raise InstagramCollectionError('--debug-browser requires INSTAGRAM_COLLECTION_PROVIDER=playwright.', reason='configuration')
                    collector.provider.set_diagnostics(self.stdout.write)
                for index, source in enumerate(chosen):
                    if index:
                        time.sleep(policy.source_delay)
                    schedule.reserve(source)  # Fail closed before any request if persistence is unavailable.
                    counts['attempted'] += 1
                    attempted_at = timezone.now()
                    try:
                        items = collector.fetch_items(source)
                        if len(items) > limit:
                            raise InstagramCollectionError('Provider returned more items than the requested limit.', reason='invalid_items')
                        counts['fetched'] += len(items)
                        if options['dry_run']:
                            ids = set(source.raw_items.values_list('external_id', flat=True))
                            new = sum(item.external_id not in ids for item in items)
                            old = len(items) - new
                        else:
                            new, old = self._store(source, items, attempted_at, completed=True)
                            counts['created'] += new
                            counts['existing'] += old
                        schedule.complete(source, new)
                        counts['successful'] += 1
                        counts['no_new'] += int(new == 0)
                        self.stdout.write(f'@{source.username}: {len(items)} fetched, {new} new, {old} existing' + (' (dry-run; DB unchanged).' if options['dry_run'] else '.'))
                    except Exception as error:
                        reason = error.reason if isinstance(error, InstagramCollectionError) else ('invalid_items' if isinstance(error, ValidationError) else 'internal_error')
                        stop = isinstance(error, InstagramCollectionError) and error.stop_run
                        # Persist cooldown before DB cleanup/partial-progress processing can fail.
                        schedule.failed(source, reason, stop)
                        counts['failed'] += 1
                        if stop and error.partial_items:
                            if len(error.partial_items) <= limit and not options['dry_run']:
                                try:
                                    new, old = self._store(source, error.partial_items, attempted_at, completed=False)
                                    counts['created'] += new
                                    counts['existing'] += old
                                    counts['fetched'] += len(error.partial_items)
                                except Exception:
                                    self.stderr.write('Partial current-source items could not be committed; earlier commits retained.')
                        # Fixed diagnostic code only: never emit provider exception/body/session details.
                        logger.warning('Instagram source #%s failed: reason=%s; stop_run=%s; committed raw items retained.', source.pk, reason, stop)
                        self.stderr.write(f'@{source.username}: reason={reason}; no retry; completed source commits retained.')
                        if isinstance(error, InstagramCollectionError) and error.diagnostics:
                            self.stderr.write('Instagram failure diagnostics: ' + json.dumps(error.diagnostics, ensure_ascii=False))
                        if stop:
                            self.stdout.write(f'Instagram stopped immediately: reason={reason}; cooldown until {timestamp(schedule.data["global_until"])}; remaining selected sources not attempted.')
                            break
                self.result_status = 'partial_failure' if counts['failed'] else 'ok'
        except InstagramCollectionError as error:
            self.result_status = error.reason
            # Configuration/state messages are fixed strings; include no supplied values/paths.
            self.stderr.write(str(error))
            raise CommandError(f'Instagram status: {error.reason}; no further requests.') from None
        finally:
            if collector:
                collector.close()
            self.stdout.write(f'Processed sources: {counts["attempted"]}\nFetched items: {counts["fetched"]}\nNew RawSourceItems: {counts["created"]}\n'
                              f'Existing items skipped: {counts["existing"]}\nFailures: {counts["failed"]}\nSources attempted: {counts["attempted"]}\n'
                              f'Successful: {counts["successful"]}\nNo new posts: {counts["no_new"]}\nFailed: {counts["failed"]}\nRaw items created: {counts["created"]}\nInstagram status: {self.result_status}')
        if counts['failed']:
            raise CommandError('Instagram collection ended with failures/partial progress; committed raw items retained.')
