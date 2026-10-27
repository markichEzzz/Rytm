from decimal import Decimal
from importlib import import_module
from unittest.mock import Mock, patch
from django.apps import apps
from django.contrib import admin
from django.db import connection
from django.forms.models import model_to_dict
from django.test import TestCase, RequestFactory
from django.utils import timezone
from datetime import timedelta
import json

from events.models import Event
from events.pricing import PriceStatus
from events.serializers import EventSerializer
from . import tests as fixtures
from .admin import EventCandidateAdmin
from .extraction import candidate_from_result
from .extractors.contracts import ExtractionResult
from .extractors.parsers import parse_price
from .forms import EventCandidateAdminForm
from .geocoding import GeocodingResult, GeocodingError, NominatimProvider, resolve_candidate_location, location_query
from .models import EventCandidate, LocationLookup
from .services import approve_candidate, build_candidate_description


class LocationPriceTests(TestCase):
    setUp = fixtures.IngestionTests.setUp
    candidate = fixtures.IngestionTests.candidate

    def test_price_extraction_states_and_minimum_note(self):
        for text, status, amount in [('500 грн', 'paid', '500'), ('від 700 грн', 'paid', '700'),
             ('безкоштовно', 'free', '0'), ('вхід вільний', 'free', '0'), ('free entry', 'free', '0'),
             ('Ціну уточнюйте', 'unknown', None), ('500 грн або 700 грн', 'unknown', None),
             ('безкоштовно 700 грн', 'unknown', None), ('Unreadable OCR ???', 'unknown', None)]:
            with self.subTest(text=text):
                price, notes = parse_price(text)
                result = ExtractionResult(is_event=True, price=price, extraction_notes=' '.join(notes))
                candidate = candidate_from_result(self.raw, result)
                self.assertEqual(candidate.price_status, status)
                self.assertEqual(candidate.price, Decimal(amount) if amount is not None else None)
                if text.startswith('від'):
                    self.assertIn('Minimum', candidate.extraction_notes)

    def test_unknown_approval_public_contract_and_description(self):
        candidate = self.candidate(price=None)
        event, _ = approve_candidate(candidate, self.reviewer)
        self.assertEqual((event.price_status, event.price), ('unknown', None))
        self.assertNotIn('безкоштовний', event.description)
        self.assertEqual(EventSerializer(event).data['price_status'], 'unknown')
        self.assertIsNone(EventSerializer(event).data['price'])

    def test_moderator_price_form_and_public_serializer_states(self):
        candidate = self.candidate(price=None)
        for status, amount in [('paid', '700'), ('free', ''), ('unknown', '500')]:
            form = EventCandidateAdminForm(data={**model_to_dict(candidate), 'price_status':status, 'price':amount}, instance=candidate)
            self.assertTrue(form.is_valid(), form.errors)
            candidate = form.save()
            self.assertEqual(candidate.price_status, status)
            self.assertEqual(candidate.price, Decimal('700') if status == 'paid' else Decimal(0) if status == 'free' else None)
        form = EventCandidateAdminForm(data={**model_to_dict(candidate), 'price_status':'paid', 'price':''}, instance=candidate)
        self.assertFalse(form.is_valid())
        self.assertIn('price', form.errors)
        for amount, status in [(None, 'unknown'), (Decimal(0), 'free'), (Decimal('700'), 'paid')]:
            event = Event(price=amount)
            self.assertEqual(EventSerializer(event).data['price_status'], status)
            serializer = EventSerializer(data={'price':amount}, partial=True, instance=event)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data['price_status'], status)
        serializer = EventSerializer(data={'price_status':'paid'}, partial=True, instance=Event(price=None))
        self.assertFalse(serializer.is_valid())

    def test_legacy_migration_preserves_free_public_events_and_unknown_candidates(self):
        candidate = self.candidate(price=None)
        event, _ = approve_candidate(candidate, self.reviewer)
        Event.objects.filter(pk=event.pk).update(price=None, price_status='unknown')
        migration = import_module('events.migrations.0003_event_price_status_alter_event_price')
        migration.preserve_legacy_prices(apps, Mock(connection=connection))
        event.refresh_from_db()
        self.assertEqual((event.price_status, event.price), ('free', Decimal(0)))
        migration = import_module('ingestion.migrations.0004_locationlookup_eventcandidate_price_status')
        migration.classify_candidate_prices(apps, Mock(connection=connection))
        candidate.refresh_from_db()
        self.assertEqual((candidate.price_status, candidate.price), ('unknown', None))

    def test_location_success_and_manual_override(self):
        candidate = self.candidate(latitude=None, longitude=None, address='вул. Лесі Українки, 1')
        provider = Mock()
        provider.geocode.return_value = GeocodingResult(49.84, 24.03, 'Lviv')
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_called_once_with('вул. Лесі Українки, 1, Львів, Україна')
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        self.assertIn('geocoded: from candidate address', model_admin.location_provenance(candidate))
        html = model_admin.location_picker(candidate)
        self.assertIn('data-latitude="49.84"', html)
        self.assertIn('data-longitude="24.03"', html)
        candidate.latitude, candidate.longitude = 49.85, 24.04
        request = RequestFactory().post('/')
        request.user = self.reviewer
        model_admin.save_model(request, candidate, Mock(changed_data=['latitude', 'longitude']), True)
        resolve_candidate_location(candidate, self.reviewer, provider)
        self.assertEqual(provider.geocode.call_count, 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, 49.85)
        self.assertIn('Moderator selected', model_admin.location_provenance(candidate))

    def test_failure_outside_bounds_and_ambiguity_leave_manual_workflow(self):
        for result in [GeocodingResult(50.45, 30.52), GeocodingError('ambiguous'), GeocodingError('provider_unavailable'), None]:
            candidate = self.candidate(latitude=None, longitude=None)
            provider = Mock()
            if isinstance(result, Exception):
                provider.geocode.side_effect = result
            else:
                provider.geocode.return_value = result
            resolve_candidate_location(candidate, self.reviewer, provider)
            candidate.refresh_from_db()
            self.assertIsNone(candidate.latitude)
            self.assertIsNone(candidate.longitude)
            self.assertIn('[Location]', candidate.extraction_notes)
            self.assertIn('candidate-location-map', EventCandidateAdmin(EventCandidate, admin.site).location_picker(candidate))
        self.assertFalse(Event.objects.exists())

    def response(self, rows):
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(rows).encode()]
        return response

    def test_nominatim_cached_identified_bounded_and_rate_limited(self):
        rows = [{'lat':'49.84', 'lon':'24.03', 'display_name':'Lviv'}]
        provider = NominatimProvider()
        with patch('ingestion.geocoding.requests.get', return_value=self.response(rows)) as get:
            first = provider.geocode('One address, Львів, Україна')
            self.assertEqual(provider.geocode('One address, Львів, Україна'), first)
            self.assertEqual(get.call_count, 1)
            kwargs = get.call_args.kwargs
            self.assertIn('Rytm', kwargs['headers']['User-Agent'])
            self.assertEqual(kwargs['timeout'], (3, 7))
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['params']['limit'], 2)
            with self.assertRaisesMessage(GeocodingError, 'rate_limited'):
                provider.geocode('Another address')
            self.assertEqual(get.call_count, 1)

    def test_nominatim_ambiguity_failure_and_unsupported_cached(self):
        provider = NominatimProvider()
        for i, rows in enumerate([[{'lat':'49.84', 'lon':'24.03'}] * 2, [{'lat':'50.45', 'lon':'30.52'}], []]):
            LocationLookup.objects.all().update(looked_up_at=timezone.now() - timedelta(seconds=2))
            with patch('ingestion.geocoding.requests.get', return_value=self.response(rows)) as get:
                for _ in range(2):
                    with self.assertRaises(GeocodingError):
                        provider.geocode(f'Address {i}')
                self.assertEqual(get.call_count, 1)
        LocationLookup.objects.all().update(looked_up_at=timezone.now() - timedelta(seconds=2))
        with patch('ingestion.geocoding.requests.get', side_effect=RuntimeError('SECRET')) as get:
            with self.assertRaisesMessage(GeocodingError, 'provider_unavailable'):
                provider.geocode('Failure')
            with self.assertRaisesMessage(GeocodingError, 'provider_unavailable'):
                provider.geocode('Failure')
            self.assertEqual(get.call_count, 1)

    def test_query_prefers_address_and_supports_venue_without_inventing_address(self):
        candidate = self.candidate(address='вул. Лесі Українки, 1', venue_name='Uncertain venue')
        self.assertNotIn('Uncertain', location_query(candidate))
        candidate.address = ''
        self.assertIn('Uncertain venue', location_query(candidate))
        self.assertEqual(candidate.address, '')
