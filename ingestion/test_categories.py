from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone
from events.models import Event
from .models import EventCandidate
from .extractors.categories import classify_category, category_supported
from .extractors.contracts import CATEGORIES, EXTRACTION_JSON_SCHEMA, ExtractionContext, KYIV
from .extractors.parsers import deterministic_extract


class CategoryExtractionTests(SimpleTestCase):
    def test_shared_choices_and_contract_include_all_existing_and_new_values(self):
        expected = {'restaurants', 'museums', 'nature', 'party', 'routes', 'education', 'performance', 'concert'}
        self.assertEqual(CATEGORIES, expected)
        self.assertEqual(dict(Event.CATEGORY_CHOICES), dict(EventCandidate._meta.get_field('category').choices))
        self.assertEqual(set(EXTRACTION_JSON_SCHEMA['properties']['category']['enum']), expected | {None})

    def test_concrete_formats_create_correct_deterministic_category(self):
        cases = [
            ('Майстер-клас з кераміки', 'education'), ('Майстерклас з малювання', 'education'),
            ('Воркшоп з фотографії', 'education'), ('Workshop photography', 'education'),
            ('Лекція про архітектуру Львова', 'education'), ('Lecture architecture', 'education'),
            ('Вистава у театрі', 'performance'), ('Показ документального фільму', 'performance'),
            ('Кінопоказ просто неба', 'performance'), ('Movie screening', 'performance'),
            ('Rock band live', 'concert'), ('Сольний концерт', 'concert'), ('Jazz live', 'concert'),
            ('Live music', 'concert'), ('Симфонічний оркестр', 'concert'),
            ('DJ night', 'party'), ('Techno rave', 'party'), ('Танцювальна вечірка', 'party'),
            ('Live concert + afterparty', 'concert'), ('Main afterparty with live concert', 'party'),
            ('DJ party with a guest band', 'party'), ('Live concert with guest DJ', 'concert'),
        ]
        future = timezone.now().astimezone(KYIV) + timedelta(days=7)
        context = ExtractionContext(reference_time=timezone.now().astimezone(KYIV), source_username='test', source_display_name='Test')
        for title, category in cases:
            with self.subTest(title=title):
                self.assertEqual(classify_category(title), category)
                self.assertTrue(category_supported(category, title))
                raw = SimpleNamespace(raw_text=f'{title}\n{future:%d.%m.%Y} 19:00', published_at=context.reference_time, raw_metadata={})
                result = deterministic_extract(raw, context)
                self.assertTrue(result.is_event)
                self.assertEqual(result.category, category)

    def test_bare_live_does_not_imply_music_or_party(self):
        self.assertIsNone(classify_category('Live stream Q&A'))
        self.assertFalse(category_supported('concert', 'Live stream Q&A'))
        self.assertFalse(category_supported('party', 'Live concert'))
