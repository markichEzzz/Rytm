"""Missing price is explicitly unknown, including safe re-extraction repairs."""
from decimal import Decimal
import json

from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from .extraction import extract_raw_source_item, enrich_candidate_pricing
from .models import EventCandidate
from . import test_extraction as extraction_fixtures
from . import test_multi_extraction as multi_fixtures


class MissingPriceExtractionTests(TestCase):
    setUp = extraction_fixtures.ExtractionTests.setUp

    def extract_price(self, text='', ocr=''):
        self.raw.raw_text = '11 жовтня о 19:00 зустрічаємось у FESTrepublic.\nLatexfauna live.\n' + text
        self.raw.raw_metadata = {'image_text': ocr}
        self.raw.save()
        candidate = extract_raw_source_item(self.raw).candidate
        self.assertIsNotNone(candidate)
        return candidate

    def test_caption_no_price_is_unknown_and_null(self):
        candidate = self.extract_price()
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_ocr_no_price_is_unknown_and_null(self):
        candidate = self.extract_price(ocr='Latexfauna live FESTrepublic')
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_unreadable_and_conflicting_prices_never_become_free(self):
        for text in ('Квитки ??? грн', '500 грн або 700 грн'):
            with self.subTest(text=text):
                candidate = self.extract_price(text)
                self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))
                candidate.delete()
                self.raw.extraction_status = 'new'
                self.raw.save()

    def test_explicit_free_phrases_are_free_zero(self):
        for text in ('безкоштовно', 'вхід вільний', 'free entry'):
            with self.subTest(text=text):
                candidate = self.extract_price(text)
                self.assertEqual((candidate.price_status, candidate.price), ('free', Decimal('0')))
                candidate.delete()
                self.raw.extraction_status = 'new'
                self.raw.save()

    def test_numeric_and_minimum_prices_are_paid(self):
        for text, expected in [('500 грн', Decimal('500')), ('від 700 грн', Decimal('700'))]:
            with self.subTest(text=text):
                candidate = self.extract_price(text)
                self.assertEqual((candidate.price_status, candidate.price), ('paid', expected))
                if text.startswith('від'):
                    self.assertIn('Minimum', candidate.extraction_notes)
                candidate.delete()
                self.raw.extraction_status = 'new'
                self.raw.save()


class MissingPriceReextractionTests(TestCase):
    setUp = multi_fixtures.MultiExtractionTests.setUp
    set_events = multi_fixtures.MultiExtractionTests.set_events
    extract = multi_fixtures.MultiExtractionTests.extract

    def initial(self):
        self.set_events(1)
        candidate = self.extract().candidates[0]
        self.raw.refresh_from_db()
        return candidate

    def test_llm_null_is_explicit_unknown(self):
        candidate = self.initial()
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_reextract_repairs_untouched_blank_status(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(price_status='')
        outcome = self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(outcome.pricing_enriched, 1)
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_reextract_preserves_complete_candidate_pricing(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(price=0, price_status='free')
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.price_status, candidate.price), ('free', Decimal('0')))

    def test_all_manual_price_statuses_are_preserved(self):
        candidate = self.initial()
        for status, price in [('paid', Decimal('500')), ('free', Decimal('0')), ('unknown', None)]:
            with self.subTest(status=status):
                EventCandidate.objects.filter(pk=candidate.pk).update(price=price, price_status=status,
                    extraction_notes='[Pricing] Moderator edited: price, price_status')
                candidate.refresh_from_db()
                self.assertFalse(enrich_candidate_pricing(candidate, Decimal('700')))
                self.extract(reextract=True)
                candidate.refresh_from_db()
                self.assertEqual((candidate.price_status, candidate.price), (status, price))

    def test_historical_admin_status_edit_is_preserved(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(price=0, price_status='free')
        user = get_user_model().objects.create_user(username='price-reviewer')
        LogEntry.objects.create(user=user, content_type=ContentType.objects.get_for_model(EventCandidate),
            object_id=str(candidate.pk), object_repr=candidate.title, action_flag=CHANGE,
            change_message=json.dumps([{'changed': {'fields': [str(EventCandidate._meta.get_field('price_status').verbose_name)]}}]))
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.price_status, candidate.price), ('free', Decimal('0')))

    def test_explicit_force_prices_overrides_manual_pricing(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(price=500, price_status='paid',
            extraction_notes='[Pricing] Moderator edited: price_status')
        self.extract(reextract=True, force_prices=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_reextract_dry_run_does_not_change_pricing(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(price_status='')
        before = list(EventCandidate.objects.values())
        outcome = self.extract(reextract=True, dry_run=True)
        self.assertEqual(outcome.pricing_enriched, 1)
        self.assertEqual(list(EventCandidate.objects.values()), before)

    def test_legacy_identity_repairs_only_safe_missing_status(self):
        candidate = self.initial()
        EventCandidate.objects.filter(pk=candidate.pk).update(source_fingerprint='', price_status='')
        self.extract(reextract=True, dry_run=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.price_status, '')
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))
        self.assertEqual(EventCandidate.objects.count(), 1)
