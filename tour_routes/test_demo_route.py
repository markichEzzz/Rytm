from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from rest_framework.test import APITestCase

from tour_routes.management.commands.seed_demo_route import DEMO_KEY, DESCRIPTION, STOPS, TITLE
from tour_routes.models import TourRoute, TourRouteStop


class DemoRouteTests(APITestCase):
    def seed(self):
        call_command('seed_demo_route', stdout=StringIO())
        return TourRoute.objects.get(demo_key=DEMO_KEY)

    def test_creates_exactly_one_published_route_with_four_ordered_landmarks(self):
        route = self.seed()
        self.assertEqual(TourRoute.objects.count(), 1)
        self.assertEqual(route.title, TITLE)
        self.assertEqual(route.description, DESCRIPTION)
        self.assertEqual(route.route_type, 'history')
        self.assertEqual(route.difficulty, 'easy')
        self.assertEqual(route.estimated_duration_minutes, 60)
        self.assertTrue(route.is_published)
        self.assertIsNone(route.submitted_by_id)
        self.assertEqual(list(route.stops.values_list('order', flat=True)), [1, 2, 3, 4])
        self.assertEqual(list(route.stops.values_list('title', flat=True)), [s['title'] for s in STOPS])
        self.assertEqual(TourRouteStop.objects.count(), 4)
        self.assertEqual(list(route.stops.values_list('source_url', flat=True)), [s['source_url'] for s in STOPS])

    def test_repeated_runs_update_same_ids_and_restore_seed_content(self):
        route = self.seed()
        ids = list(route.stops.values_list('pk', flat=True))
        route.title = 'Edited demo'; route.save()
        stop = route.stops.first(); stop.latitude = 0; stop.save()
        again = self.seed()
        self.assertEqual(route.pk, again.pk)
        self.assertEqual(TourRoute.objects.count(), 1)
        self.assertEqual(list(again.stops.values_list('pk', flat=True)), ids)
        self.assertEqual(again.title, TITLE)
        self.assertEqual(again.stops.first().latitude, STOPS[0]['latitude'])
        self.assertEqual(TourRouteStop.objects.count(), 4)

    def test_does_not_modify_other_routes_or_stops(self):
        other = TourRoute.objects.create(title='Curated record', demo_key='lviv-demo-classic')
        stop = TourRouteStop.objects.create(route=other, order=1, title='Keep', latitude=49, longitude=24)
        self.seed(); self.seed()
        other.refresh_from_db(); stop.refresh_from_db()
        self.assertEqual(other.title, 'Curated record')
        self.assertFalse(other.is_published)
        self.assertEqual(stop.title, 'Keep')
        self.assertEqual(TourRoute.objects.count(), 2)
        self.assertEqual(TourRouteStop.objects.count(), 5)

    def test_extra_stops_refuse_without_deletion_or_partial_update(self):
        route = self.seed()
        TourRouteStop.objects.create(route=route, order=5, title='Keep extra', latitude=49, longitude=24)
        route.title = 'Keep curated edit'; route.save()
        with self.assertRaises(CommandError): self.seed()
        route.refresh_from_db()
        self.assertEqual(route.title, 'Keep curated edit')
        self.assertTrue(route.is_published)
        self.assertEqual(route.stops.count(), 5)

    def test_valid_lviv_coordinates_and_public_api_contract(self):
        route = self.seed()
        response = self.client.get('/api/routes/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data], [route.pk])
        self.assertEqual(response.data[0]['stop_count'], 4)
        self.assertEqual(response.data[0]['distance_kind'], 'straight_line')
        self.assertTrue(700 < response.data[0]['distance_meters'] < 1100)
        self.assertIsNone(response.data[0]['cover_image'])
        detail = self.client.get(f'/api/routes/{route.pk}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['title'], TITLE)
        self.assertEqual(detail.data['description'], DESCRIPTION)
        self.assertEqual([stop['order'] for stop in detail.data['stops']], [1, 2, 3, 4])
        for stop, expected in zip(detail.data['stops'], STOPS):
            self.assertEqual(stop['title'], expected['title'])
            self.assertEqual(stop['description'], expected['description'])
            self.assertEqual(stop['source_url'], expected['source_url'])
            self.assertGreater(len(stop['description']), 120)
            self.assertEqual((stop['latitude'], stop['longitude']), (expected['latitude'], expected['longitude']))
            self.assertTrue(49.83 < stop['latitude'] < 49.85)
            self.assertTrue(24.02 < stop['longitude'] < 24.04)
        route.is_published = False; route.save()
        self.assertEqual(self.client.get('/api/routes/').data, [])
        self.assertEqual(self.client.get(f'/api/routes/{route.pk}/').status_code, 404)

    def test_failed_publication_rolls_back_all_seed_writes(self):
        with patch.object(TourRoute, 'validate_for_publication', side_effect=RuntimeError('validation failed')):
            with self.assertRaises(RuntimeError): self.seed()
        self.assertEqual(TourRoute.objects.count(), 0)
        self.assertEqual(TourRouteStop.objects.count(), 0)

    def test_nonlocal_database_requires_explicit_operator_flag_before_queries(self):
        connection = connections['default']
        with patch.dict(connection.settings_dict, HOST='db.example.invalid'):
            with self.assertNumQueries(0), self.assertRaises(CommandError): self.seed()
            call_command('seed_demo_route', allow_nonlocal_database=True, stdout=StringIO())
        self.assertEqual(TourRoute.objects.filter(demo_key=DEMO_KEY).count(), 1)
