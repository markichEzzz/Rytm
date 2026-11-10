from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from tour_routes.admin import RouteStopInline
from tour_routes.management.commands.seed_demo_route import STOPS
from tour_routes.models import TourRouteStop
from tour_routes.serializers import RouteStopSerializer


class RouteStopSourceTests(SimpleTestCase):
    def test_optional_source_is_backwards_compatible(self):
        stop = TourRouteStop(order=1, title='Stop', latitude=49.84, longitude=24.03)
        self.assertEqual(stop.source_url, '')
        self.assertEqual(RouteStopSerializer(stop).data['source_url'], '')

    def test_source_is_exposed_and_editable_in_admin(self):
        url = STOPS[0]['source_url']
        stop = TourRouteStop(order=1, title='Stop', latitude=49.84, longitude=24.03, source_url=url)
        self.assertEqual(RouteStopSerializer(stop).data['source_url'], url)
        self.assertIn('source_url', RouteStopInline.fields)

    def test_source_only_accepts_web_urls(self):
        field = TourRouteStop._meta.get_field('source_url')
        for url in ('https://lviv.travel/example', 'http://example.org/place', ''):
            self.assertEqual(field.clean(url, None), url)
        for url in ('javascript:alert(1)', 'file:///tmp/place', 'ftp://example.org/place', 'not a URL'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                field.clean(url, None)

    def test_all_demo_stops_have_reviewed_sources(self):
        self.assertEqual([s['source_url'] for s in STOPS], [
            'https://lia.lvivcenter.org/en/objects/opera-house/',
            'https://lviv.travel/en/places/attractions/ploshcha-rinok',
            'https://lia.lvivcenter.org/en/objects/dominican-church/',
            'https://www.lvivcenter.org/en/spatial/powder-tower-2/',
        ])
