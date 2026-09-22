from django.contrib.gis.geos import Point
from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from rest_framework import status
from .models import Event, Organizer

class EventAPITests(APITestCase):
    def setUp(self):
        # Створюємо тестового користувача для авторизації
        self.user = User.objects.create_user(username='testorg', password='password123')
        
        # Отримуємо JWT-токен для нього
        response = self.client.post('/api/token/', {'username': 'testorg', 'password': 'password123'})
        self.token = response.data['access']
        
        # Створюємо дві події в базі: одну затверджену, іншу — на модерації
        self.event_approved = Event.objects.create(
            title="Затверджена тусовка",
            description="Опис",
            category="party",
            start_time="2026-10-01T18:00:00Z",
            location=Point(24.0297, 49.8397), # Координати у форматі PostGIS
            address="Львів, Центр",
            status="approved"
        )
        
        self.event_pending = Event.objects.create(
            title="Новий музей",
            description="Опис",
            category="museums",
            start_time="2026-10-02T10:00:00Z",
            location=Point(24.0300, 49.8400),
            address="Львів, Музей",
            status="pending"
        )

    def test_get_approved_events_only(self):
        """Перевіряємо, що стрічка подій показує ТІЛЬКИ затверджені записи"""
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1) # Має повернутись 1 подія, а не 2
        self.assertEqual(response.data[0]['title'], "Затверджена тусовка")

    def test_category_filter(self):
        """Перевіряємо фільтр за категорією"""
        response = self.client.get('/api/events/?category=party')
        self.assertEqual(len(response.data), 1)
        
        # Перевіряємо категорію, подія якої ще на модерації (не повинна виводитись)
        response_empty = self.client.get('/api/events/?category=museums')
        self.assertEqual(len(response_empty.data), 0)

    def test_create_event_authenticated(self):
            """Перевіряємо, що авторизований організатор може створити подію (статус pending)"""
            self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)
            
            # Створюємо СПРАВЖНЮ картинку 1x1 піксель у пам'яті
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
                content_type='image/jpeg'
            )
            
            data = {
                "title": "Нова подія через API",
                "description": "Тест форми",
                "category": "restaurants",
                "start_time": "2026-11-01T12:00:00Z",
                "location": "POINT(24.0 49.8)", 
                "address": "Тестова адреса",
                "photo": dummy_photo
            }
            
            response = self.client.post('/api/events/', data, format='multipart')
                
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data['status'], 'pending')

    def test_create_event_unauthenticated(self):
        """Перевіряємо, що аноніми отримують помилку 401 при спробі додати подію"""
        data = {"title": "Спам"}
        response = self.client.post('/api/events/', data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)