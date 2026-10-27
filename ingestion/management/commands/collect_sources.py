from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Dispatch enabled Instagram and Telegram sources through compatible collectors.'

    def add_arguments(self, parser):
        parser.add_argument('--platform', choices=['instagram','telegram'])
        parser.add_argument('--limit', type=int, default=5)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self,*args,**options):
        failures=[]
        for platform in ([options['platform']] if options['platform'] else ['instagram','telegram']):
            try:
                call_command('collect_'+platform,limit=options['limit'],dry_run=options['dry_run'],stdout=self.stdout,stderr=self.stderr)
            except CommandError:
                failures.append(platform)
        if failures:
            raise CommandError('Sources completed with failures: '+', '.join(failures))
