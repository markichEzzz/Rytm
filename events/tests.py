from django.contrib.gis.geos import Point
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import override_settings
from rest_framework.test import APITestCase, APITransactionTestCase, APIClient
from rest_framework import status
from .models import Event, Organizer


class EventAPITests(APITestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        storage = override_settings(
            MEDIA_ROOT=media.name,
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
            },
        )
        storage.enable()
        self.addCleanup(storage.disable)
        # Створюємо тестового користувача для авторизації
        self.user = User.objects.create_user(username='testorg', password='password123')

        # Отримуємо JWT-токен для нього
        response = self.client.post('/api/token/', {'username': 'testorg', 'password': 'password123'})
        self.token = response.data['access']

        now = timezone.now()

        # Майбутня затверджена подія
        self.event_approved = Event.objects.create(
            title="Затверджена тусовка",
            description="Опис",
            category="party",
            start_time=now + timedelta(days=3),
            location=Point(24.0297, 49.8397),
            address="Львів, Центр",
            status="approved",
        )

        # Майбутня подія на модерації
        self.event_pending = Event.objects.create(
            title="Новий музей",
            description="Опис",
            category="museums",
            start_time=now + timedelta(days=5),
            location=Point(24.0300, 49.8400),
            address="Львів, Музей",
            status="pending",
        )

        # Минула затверджена подія (вже не повинна повертатись)
        self.event_past = Event.objects.create(
            title="Минулий концерт",
            description="Опис",
            category="party",
            start_time=now - timedelta(days=5),
            end_time=now - timedelta(days=5) + timedelta(hours=3),
            location=Point(24.0297, 49.8397),
            address="Львів, Центр",
            status="approved",
        )

    def test_get_approved_events_only(self):
        """Стрічка подій показує ТІЛЬКИ затверджені записи"""
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [e['title'] for e in response.data]
        self.assertIn("Затверджена тусовка", titles)
        self.assertNotIn("Новий музей", titles)

    def test_past_events_excluded(self):
        """Минулі події не повертаються в стрічці"""
        response = self.client.get('/api/events/')
        titles = [e['title'] for e in response.data]
        self.assertNotIn("Минулий концерт", titles)

    def test_event_with_end_time_in_future_included(self):
        """Подія з end_time у майбутньому показується, навіть якщо start_time минув"""
        now = timezone.now()
        Event.objects.create(
            title="Поточний фестиваль",
            description="Ще не закінчився",
            category="party",
            start_time=now - timedelta(hours=2),
            end_time=now + timedelta(hours=5),
            location=Point(24.0297, 49.8397),
            address="Львів",
            status="approved",
        )
        response = self.client.get('/api/events/')
        titles = [e['title'] for e in response.data]
        self.assertIn("Поточний фестиваль", titles)

    def test_events_ordered_by_start_time(self):
        """Події відсортовані від найближчої до пізнішої"""
        now = timezone.now()
        Event.objects.create(
            title="Рання подія",
            description="Опис",
            category="party",
            start_time=now + timedelta(hours=1),
            location=Point(24.0297, 49.8397),
            address="Львів",
            status="approved",
        )
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        times = [e['start_time'] for e in response.data]
        self.assertEqual(times, sorted(times))

    def test_category_filter(self):
        """Фільтр за категорією працює"""
        response = self.client.get('/api/events/?category=party')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)
        for e in response.data:
            self.assertEqual(e['category'], 'party')

        # Категорія, подія якої ще на модерації (не повинна виводитись)
        response_empty = self.client.get('/api/events/?category=museums')
        self.assertEqual(len(response_empty.data), 0)

    def test_bbox_filter(self):
        """bbox-фільтр повертає тільки події в межах координат"""
        # Включає Львів
        response = self.client.get('/api/events/?bbox=23.9,49.7,24.2,49.9')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

        # Поза Львовом — не має подій
        response_empty = self.client.get('/api/events/?bbox=30.0,50.0,31.0,51.0')
        self.assertEqual(len(response_empty.data), 0)

    def test_event_detail(self):
        """Деталі затвердженої події повертаються успішно"""
        response = self.client.get(f'/api/events/{self.event_approved.pk}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], "Затверджена тусовка")

    def test_event_detail_pending_returns_404(self):
        """Подія з pending-статусом не доступна через detail"""
        response = self.client.get(f'/api/events/{self.event_pending.pk}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_event_detail_missing_returns_404(self):
        response = self.client.get('/api/events/999999999/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_expiration_boundaries(self):
        now = timezone.now()
        for end_time, start_time, expected in [
            (now, now - timedelta(hours=1), True),
            (now - timedelta(microseconds=1), now - timedelta(hours=1), False),
            (None, now, True),
            (None, now - timedelta(microseconds=1), False),
        ]:
            with self.subTest(end_time=end_time, start_time=start_time):
                Event.objects.filter(pk=self.event_approved.pk).update(
                    start_time=start_time, end_time=end_time,
                )
                with patch('events.views.timezone.now', return_value=now):
                    response = self.client.get('/api/events/')
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    self.event_approved.pk in [event['id'] for event in response.data],
                    expected,
                )

    def test_bbox_and_category_combined(self):
        Event.objects.create(
            title='Outside Lviv', description='Test', category='party',
            start_time=timezone.now() + timedelta(days=1),
            location=Point(30.5, 50.4), address='Kyiv', status='approved',
        )
        response = self.client.get('/api/events/', {
            'category': 'party', 'bbox': '23.9,49.77,24.15,49.9',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([event['id'] for event in response.data], [self.event_approved.pk])

    def test_response_contract_and_nullable_fields(self):
        response = self.client.get(f'/api/events/{self.event_approved.pk}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {
            'id', 'title', 'description', 'category', 'price', 'price_status', 'start_time',
            'end_time', 'location', 'latitude', 'longitude', 'address',
            'photo', 'source_url', 'organizer', 'status', 'venue',
        })
        for field in ['price', 'end_time', 'photo', 'source_url', 'organizer']:
            self.assertIsNone(response.data[field])
        self.assertAlmostEqual(response.data['latitude'], 49.8397)
        self.assertAlmostEqual(response.data['longitude'], 24.0297)

    def test_create_event_authenticated(self):
        """Авторизований організатор може створити подію (статус pending)"""
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)

        import io
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile

        image_io = io.BytesIO()
        image = Image.new('RGB', (1, 1), color='white')
        image.save(image_io, format='JPEG')
        image_io.seek(0)

        dummy_photo = SimpleUploadedFile(
            name='test_photo.jpg',
            content=image_io.read(),
            content_type='image/jpeg',
        )

        data = {
            "title": "Нова подія через API",
            "description": "Тест форми",
            "category": "restaurants",
            "start_time": (timezone.now() + timedelta(days=30)).isoformat(),
            "location": "POINT(24.0 49.8)",
            "address": "Тестова адреса",
            "photo": dummy_photo,
            "status": "approved",
        }

        response = self.client.post('/api/events/', data, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'pending')
        created_id = response.data['id']
        self.assertEqual(Event.objects.get(pk=created_id).status, 'pending')
        self.client.credentials()
        self.assertNotIn(created_id, [event['id'] for event in self.client.get('/api/events/').data])
        self.assertEqual(self.client.get(f'/api/events/{created_id}/').status_code, status.HTTP_404_NOT_FOUND)

    def test_create_event_unauthenticated(self):
        """Аноніми отримують помилку 401 при спробі додати подію"""
        data = {"title": "Спам"}
        response = self.client.post('/api/events/', data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class EventSubmissionTests(APITestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        storage = override_settings(MEDIA_ROOT=media.name, STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        })
        storage.enable()
        self.addCleanup(storage.disable)
        self.user = User.objects.create_user(username='trusted')
        self.organizer = Organizer.objects.create(
            user=self.user, name='Trusted organizer', contact_email='trusted@example.test', verified=True,
        )
        self.client.force_authenticate(self.user)

    def payload(self, **overrides):
        import io
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        image = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(image, format='JPEG')
        data = {
            'title': 'Concert in Lviv', 'description': 'A live music concert in the city center.',
            'category': 'party', 'price': '100.50',
            'start_time': (timezone.now() + timedelta(days=2)).isoformat(),
            'end_time': (timezone.now() + timedelta(days=2, hours=3)).isoformat(),
            'location': 'POINT(24.03 49.84)', 'address': 'Lviv, Market Square',
            'photo': SimpleUploadedFile('event.jpg', image.getvalue(), content_type='image/jpeg'),
            'source_url': 'https://example.com/event',
        }
        data.update(overrides)
        return data

    def submit(self, **overrides):
        return self.client.post('/api/events/', self.payload(**overrides), format='multipart')

    def test_trusted_valid_event_auto_approved_and_public(self):
        response = self.submit(status='pending', organizer=999999)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'approved')
        event = Event.objects.get(pk=response.data['id'])
        self.assertEqual(event.submitted_by, self.user)
        self.assertEqual(event.organizer, self.organizer)
        self.assertTrue(timezone.is_aware(event.created_at))
        self.client.force_authenticate(None)
        result = self.client.get('/api/events/', {'bbox': '23.9,49.77,24.15,49.9', 'category': 'party'})
        self.assertEqual([item['id'] for item in result.data], [event.pk])
        self.assertEqual(self.client.get(f'/api/events/{event.pk}/').status_code, 200)

    def test_unverified_valid_event_pending_and_private(self):
        self.organizer.verified = False
        self.organizer.save()
        response = self.submit(status='approved')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'pending')
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/events/').data, [])
        self.assertEqual(self.client.get(f"/api/events/{response.data['id']}/").status_code, 404)

    def test_account_without_profile_stays_pending(self):
        self.organizer.user = None
        self.organizer.save()
        response = self.submit()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['organizer']['id'], self.user.pk)

    def test_valid_outside_lviv_event_stays_pending(self):
        response = self.submit(location='POINT(30.5 50.4)')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'pending')

    def test_valid_short_description_stays_pending(self):
        response = self.submit(description='Short')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'pending')

    def test_equal_end_start_valid_but_pending(self):
        start = (timezone.now() + timedelta(days=1)).isoformat()
        response = self.submit(start_time=start, end_time=start)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'pending')

    def test_optional_price_and_end_allow_auto_approval(self):
        data = self.payload()
        del data['price']
        del data['end_time']
        response = self.client.post('/api/events/', data, format='multipart')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'approved')
        self.assertIsNone(response.data['price'])
        self.assertIsNone(response.data['end_time'])

    def test_invalid_data_rejected_without_consuming_cooldown(self):
        cases = [
            {'title': ''}, {'description': ''}, {'address': ''}, {'category': 'unknown'},
            {'price': '-50'}, {'price': 'abc'}, {'price': '12abc'}, {'price': '1.001'},
            {'price': 'NaN'}, {'price': 'Infinity'},
            {'start_time': (timezone.now() - timedelta(minutes=1)).isoformat()},
            {'end_time': (timezone.now() + timedelta(hours=1)).isoformat()},
            {'location': 'POINT(181 49.84)'}, {'location': 'POINT(24 91)'},
            {'location': 'POINT(NaN 49.84)'}, {'location': 'not a point'},
            {'location': 'POINT EMPTY'}, {'location': 'POINT Z(24.03 49.84 10)'},
            {'location': 'SRID=3857;POINT(24.03 49.84)'},
            {'start_time': (timezone.now() + timedelta(days=1)).replace(tzinfo=None).isoformat()},
            {'source_url': 'not a URL'}, {'photo': 'not an image'},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                response = self.submit(**changes)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Event.objects.filter(submitted_by=self.user).count(), 0)
        self.assertEqual(self.submit().status_code, 201)

    def test_second_submission_returns_429_and_retry_after(self):
        first = self.submit()
        self.assertEqual(first.status_code, 201)
        second = self.submit()
        self.assertEqual(second.status_code, 429)
        self.assertIn('You can submit another event in', str(second.data['detail']))
        self.assertGreater(int(second['Retry-After']), 0)
        self.assertLessEqual(int(second['Retry-After']), 10800)
        self.assertEqual(Event.objects.filter(submitted_by=self.user).count(), 1)

    def test_cooldown_also_applies_to_pending_submissions(self):
        self.organizer.verified = False
        self.organizer.save()
        self.assertEqual(self.submit().data['status'], 'pending')
        self.assertEqual(self.submit().status_code, 429)

    def test_cooldown_exact_boundary_and_other_account(self):
        first = self.submit()
        event_id = first.data['id']
        now = timezone.now()
        Event.objects.filter(pk=event_id).update(created_at=now - timedelta(hours=3) + timedelta(seconds=1))
        with patch('events.moderation.timezone.now', return_value=now):
            response = self.submit()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '1')
        other = User.objects.create_user(username='other')
        self.client.force_authenticate(other)
        self.assertEqual(self.submit().status_code, 201)
        self.client.force_authenticate(self.user)
        Event.objects.filter(pk=event_id).update(created_at=now - timedelta(hours=3))
        with patch('events.moderation.timezone.now', return_value=now):
            self.assertEqual(self.submit().status_code, 201)

    def test_inactive_account_cannot_submit(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.submit().status_code, 403)

    def test_client_cannot_spoof_submission_history(self):
        response = self.submit(submitted_by=999, created_at='2000-01-01T00:00:00Z')
        self.assertEqual(response.status_code, 201)
        event = Event.objects.get(pk=response.data['id'])
        self.assertEqual(event.submitted_by, self.user)
        self.assertGreater(event.created_at, timezone.now() - timedelta(minutes=1))
        self.assertNotIn('submitted_by', response.data)
        self.assertNotIn('created_at', response.data)

    def test_auto_approval_notification_runs_after_commit(self):
        with patch('events.signals._send_approval_notification') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.submit()
                self.assertEqual(response.status_code, 201)
                notify.assert_not_called()
            notify.assert_called_once()

    def test_admin_bulk_actions_preserve_manual_moderation(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from .admin import EventAdmin
        self.organizer.verified = False
        self.organizer.save()
        event = Event.objects.get(pk=self.submit().data['id'])
        event_admin = EventAdmin(Event, admin.site)
        request = RequestFactory().post('/admin/')
        queryset = Event.objects.filter(pk=event.pk)
        with patch.object(event_admin, 'message_user'), patch('events.signals._send_approval_notification') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                event_admin.approve_selected(request, queryset)
            notify.assert_called_once()
            event.refresh_from_db()
            self.assertEqual(event.status, 'approved')
            event_admin.mark_pending(request, queryset)
            event.refresh_from_db()
            self.assertEqual(event.status, 'pending')
            event_admin.reject_selected(request, queryset)
            event.refresh_from_db()
            self.assertEqual(event.status, 'rejected')


class ConcurrentSubmissionTests(APITransactionTestCase):
    """Exercise real Postgres row locking, outside TestCase's wrapping transaction."""
    payload = EventSubmissionTests.payload

    def test_parallel_first_submissions_only_create_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        user = User.objects.create_user(username='parallel')
        barrier = Barrier(2)
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media, STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        }):
            def submit():
                close_old_connections()
                try:
                    client = APIClient()
                    client.force_authenticate(user)
                    data = self.payload()
                    barrier.wait(timeout=10)
                    return client.post('/api/events/', data, format='multipart').status_code
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(results), [201, 429])
        self.assertEqual(Event.objects.filter(submitted_by=user).count(), 1)
