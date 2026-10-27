from django.core.management.base import BaseCommand
from accounts.deletion import purge_deleted_media
from accounts.models import DeletedAccountMedia

class Command(BaseCommand):
    help = 'Retry deletion of unreferenced media from deleted accounts.'
    def handle(self, **options):
        purge_deleted_media()
        self.stdout.write(f'Pending media deletions: {DeletedAccountMedia.objects.count()}')
