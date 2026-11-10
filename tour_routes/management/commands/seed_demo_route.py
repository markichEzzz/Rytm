"""One reviewed, public Lviv walking route; no network calls or schema changes."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from tour_routes.models import Difficulty, RouteType, TourRoute, TourRouteStop

DEMO_KEY = 'lviv-first-acquaintance-v1'
TITLE = 'Перше знайомство зі Львовом'
DESCRIPTION = ('Невелика прогулянка історичним центром Львова через чотири знакові локації. '
               'Підійде для першого знайомства з містом або легкої вечірньої прогулянки.')
# WGS84 landmark locations, rounded to 6 decimals; sources and verification in docs/demo-route.md.
# They identify landmarks, not guaranteed entrances or pedestrian routing instructions.
STOPS = (
    dict(title='Львівська опера', source_url='https://lia.lvivcenter.org/en/objects/opera-house/', latitude=49.843989, longitude=24.026178,
         description='Оперний театр завершує перспективу проспекту Свободи. Будівлю спорудили у 1897–1900 роках за проєктом архітектора Зиґмунта Ґорґолевського. Зупиніться перед фасадом, щоб роздивитися його скульптурне оздоблення.',
         address='проспект Свободи, 28, Львів', recommended_stop_minutes=10),
    dict(title='Площа Ринок / Львівська ратуша', source_url='https://lviv.travel/en/places/attractions/ploshcha-rinok', latitude=49.841835, longitude=24.031326,
         description='Площа Ринок — історичний центральний простір старого Львова. Посеред площі стоїть Ратуша, а навколо розташовані історичні кам’яниці. Під час прогулянки зверніть увагу на фасади будинків і вежу Ратуші.',
         address='площа Ринок, 1, Львів', recommended_stop_minutes=15),
    dict(title='Домініканський собор', source_url='https://lia.lvivcenter.org/en/objects/dominican-church/', latitude=49.842778, longitude=24.034167,
         description='Колишній домініканський костел на Музейній площі відомий бароковим фасадом і купольним силуетом. Нині це храм Пресвятої Євхаристії. Роздивіться фасад із площі, перш ніж продовжити прогулянку до Підвальної.',
         address='Музейна площа, Львів', recommended_stop_minutes=10),
    dict(title='Порохова вежа', source_url='https://www.lvivcenter.org/en/spatial/powder-tower-2/', latitude=49.842781, longitude=24.035561,
         description='Порохова вежа на Підвальній — пам’ятка оборонної архітектури XVI століття. Вона була частиною міських укріплень. Роздивіться масивні кам’яні стіни та порівняйте вежу з цивільними будівлями історичного центру.',
         address='вулиця Підвальна, 4, Львів', recommended_stop_minutes=10),
)


class Command(BaseCommand):
    help = 'Create/update one published four-stop Lviv route; defaults to local databases only.'

    def add_arguments(self, parser):
        parser.add_argument('--allow-nonlocal-database', action='store_true',
                            help='Explicit operator approval to seed the configured non-local database.')

    def handle(self, *args, **options):
        # Guard before the first DB query: an operator's .env may point at production.
        host = str(connections['default'].settings_dict.get('HOST') or '')
        local = host in {'', 'localhost', '127.0.0.1', '::1'} or host.startswith('/')
        if not local and not options['allow_nonlocal_database']:
            raise CommandError('Non-local database refused. Select a local database, or after review '
                               'explicitly pass --allow-nonlocal-database. No data was changed.')
        with transaction.atomic():
            route, created = TourRoute.objects.get_or_create(demo_key=DEMO_KEY)
            route = TourRoute.objects.select_for_update().get(pk=route.pk)
            stops = list(route.stops.select_for_update())
            # Never adopt a user submission or delete extra/curated stops to enforce the seed.
            if route.submitted_by_id is not None or any(stop.order not in range(1, 5) for stop in stops):
                raise CommandError('Demo record has an owner or unexpected stop orders. Review in admin; no data was changed.')
            route.is_published = False  # Staging within this transaction; no partial public route is visible.
            route.title = TITLE
            route.short_description = DESCRIPTION
            route.description = DESCRIPTION
            route.route_type = RouteType.HISTORY  # Existing type describes theme, not transport mode.
            route.estimated_duration_minutes = 60
            route.difficulty = Difficulty.EASY
            route.save()
            for order, fields in enumerate(STOPS, 1):
                TourRouteStop.objects.update_or_create(route=route, order=order, defaults=fields)
            route.is_published = True
            route.save()  # Runs existing metadata, stop and publication validation.
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} demo route id={route.pk}; published; 4 ordered stops. '
                                             f'API: /api/routes/{route.pk}/'))
