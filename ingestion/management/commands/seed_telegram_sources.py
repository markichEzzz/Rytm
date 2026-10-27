from django.core.management.base import BaseCommand
from ingestion.models import SourceAccount


class Command(BaseCommand):
    help = 'Idempotently seed the initial public Telegram channels, preserving existing settings.'

    def handle(self,*args,**options):
        created=0
        for username in ('kudypity_lviv','lvivgo_afisha'):
            _, added = SourceAccount.objects.get_or_create(platform='telegram',username=username,
                defaults={'source_url':f'https://t.me/{username}'})
            created += int(added)
        self.stdout.write(f'Added {created} Telegram source accounts.')
