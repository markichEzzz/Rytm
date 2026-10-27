from datetime import timedelta
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APITestCase
from . import tests as fixtures
from .models import Event
from .serializers import EventSerializer


class NewCategoryAPITests(APITestCase):
    setUp = fixtures.EventSubmissionTests.setUp
    payload = fixtures.EventSubmissionTests.payload

    def test_organizer_submissions_accept_all_new_values(self):
        for category in ('education', 'performance', 'concert'):
            with self.subTest(category=category):
                response = self.client.post('/api/events/', self.payload(category=category, title=category), format='multipart')
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['category'], category)
                event = Event.objects.get(pk=response.data['id'])
                event.full_clean()
                self.assertEqual(EventSerializer(event).data['category'], category)
                Event.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(hours=4))

    def test_public_list_and_category_filters(self):
        for category in ('education', 'performance', 'concert', 'party'):
            Event.objects.create(title=category, description='Event', category=category, status='approved',
                start_time=timezone.now() + timedelta(days=7), location=Point(24.03, 49.84), address='Lviv')
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({entry['category'] for entry in response.data}, {'education', 'performance', 'concert', 'party'})
        for category in ('education', 'performance', 'concert'):
            with self.subTest(category=category):
                response = self.client.get('/api/events/', {'category': category})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([entry['category'] for entry in response.data], [category])
