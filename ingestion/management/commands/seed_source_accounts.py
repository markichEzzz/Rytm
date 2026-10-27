import json
from importlib.resources import files

from django.core.management.base import BaseCommand
from django.db import transaction

from ingestion.models import SourceAccount


class Command(BaseCommand):
    help = 'Seed editable Instagram accounts from the bundled source list; preserve existing settings.'

    def handle(self, *args, **options):
        usernames = json.loads(files('ingestion').joinpath('data/instagram_sources.json').read_text())
        created = 0
        with transaction.atomic():
            for username in usernames:
                _, added = SourceAccount.objects.get_or_create(
                    platform=SourceAccount.Platform.INSTAGRAM, username=username,
                    defaults={'source_url': f'https://www.instagram.com/{username}/'},
                )
                created += int(added)
        self.stdout.write(self.style.SUCCESS(f'Added {created} source accounts; existing records were preserved.'))
