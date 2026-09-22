import os
from django.apps import AppConfig
from django.conf import settings
import firebase_admin
from firebase_admin import credentials

class EventsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'events'

    def ready(self):
        # Ініціалізуємо Firebase лише один раз
        if not firebase_admin._apps:
            key_path = os.path.join(settings.BASE_DIR, 'firebase-key.json')
            if os.path.exists(key_path):
                cred = credentials.Certificate(key_path)
                firebase_admin.initialize_app(cred)
        
        # Підключаємо наші сигнали
        import events.signals