import json
from importlib.resources import files

from django.core.management.base import BaseCommand
from django.db import transaction

from ingestion.models import SourceAccount
from ingestion.instagram_urls import canonical_profile_url

# Only former bundled names may be deactivated. Custom operator accounts are untouched.
OBSOLETE_BUNDLED = frozenset({
    'jamfactory.artcenter', 'malevich_club', 'cult.comedy.hall', 'leskurbastheatre',
    'urbanhistorycenter', 'mad_barshouse', 'streetfood_sogood', 'zankovetskatheatre',
    'pershyiteatr', 'g.zone_project', 'dobre_party', 'split.night.club',
})


class Command(BaseCommand):
    help = 'Seed current canonical Instagram accounts; preserve settings and deactivate obsolete bundled names.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Preview source synchronization without writes.')

    def handle(self, *args, **options):
        usernames = json.loads(files('ingestion').joinpath('data/instagram_sources.json').read_text())
        created = normalized = disabled = 0
        with transaction.atomic():
            for username in usernames:
                url = canonical_profile_url(username)
                source = SourceAccount.objects.filter(platform='instagram', username__iexact=username).first()
                if source is None:
                    created += 1
                    if not options['dry_run']:
                        SourceAccount.objects.get_or_create(platform='instagram', username=username, defaults={'source_url': url})
                elif source.source_url != url:
                    normalized += 1
                    if not options['dry_run']:
                        source.source_url = url
                        source.save(update_fields=['source_url', 'updated_at'])
            obsolete = SourceAccount.objects.filter(platform='instagram', username__in=OBSOLETE_BUNDLED, enabled=True)
            disabled = obsolete.count()
            if not options['dry_run']:
                obsolete.update(enabled=False)
        self.stdout.write(self.style.SUCCESS(
            f'{"Would add" if options["dry_run"] else "Added"} {created} source accounts; '
            f'{"would canonicalize" if options["dry_run"] else "canonicalized"} {normalized}; '
            f'{"would deactivate" if options["dry_run"] else "deactivated"} {disabled} obsolete bundled accounts. '
            'Existing settings, records and history preserved.'
        ))
