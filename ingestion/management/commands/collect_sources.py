from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Collect platforms independently and explicitly report partial success.'

    def add_arguments(self, parser):
        parser.add_argument('--platform', choices=['instagram', 'telegram'])
        parser.add_argument('--limit', type=int, default=None, help='Explicit per-source limit; omission preserves each platform\'s defaults.')
        parser.add_argument('--instagram-limit-sources', type=int)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        results = {}
        for platform in ([options['platform']] if options['platform'] else ['instagram', 'telegram']):
            kwargs = dict(dry_run=options['dry_run'], stdout=self.stdout, stderr=self.stderr)
            if options['limit'] is not None:
                kwargs['limit'] = options['limit']
            if platform == 'instagram' and options['instagram_limit_sources'] is not None:
                kwargs['limit_sources'] = options['instagram_limit_sources']
            try:
                # No shared transaction: a platform failure cannot roll back another's raw items.
                call_command('collect_' + platform, **kwargs)
                results[platform] = 'OK (see collector status/counts above)'
            except Exception:
                # Do not echo arbitrary provider error bodies. Continue the OTHER platform, never retry this one.
                results[platform] = 'FAILED (committed partial progress retained)'
        summary = '; '.join(f'{name}={status}' for name, status in results.items())
        self.stdout.write('Platform results: ' + summary)
        if any(status.startswith('FAILED') for status in results.values()):
            raise CommandError('Partial collection: ' + summary)
