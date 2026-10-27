import json
from decimal import Decimal
from unittest.mock import Mock, patch
from django.contrib import admin
from django.forms.models import model_to_dict
from django.test import TestCase, RequestFactory
from django.urls import reverse
from . import tests as fixtures
from .admin import EventCandidateAdmin
from .forms import EventCandidateAdminForm
from .geocoding import (GeocodingResult, GeocodingError, known_venue_location,
                       location_queries, normalize_location_text, resolve_candidate_location)
from .services import approve_candidate, build_candidate_description
from .models import EventCandidate


class ResolverUXTests(TestCase):
    setUp = fixtures.IngestionTests.setUp
    candidate = fixtures.IngestionTests.candidate

    def test_free_unknown_remove_stale_hidden_values_even_invalid_input(self):
        for status, expected in [('free', Decimal('0.00')), ('unknown', None)]:
            for stale in ['700', 'not numeric', '-100']:
                candidate = self.candidate()
                form = EventCandidateAdminForm(data={**model_to_dict(candidate), 'price_status':status, 'price':stale}, instance=candidate)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                candidate.refresh_from_db()
                self.assertEqual(candidate.price, expected)
                self.assertEqual(candidate.price_status, status)

    def test_paid_requires_nonnegative_numeric_price(self):
        candidate = self.candidate()
        for amount in ['', '-1', 'nan']:
            form = EventCandidateAdminForm(data={**model_to_dict(candidate), 'price_status':'paid', 'price':amount}, instance=candidate)
            self.assertFalse(form.is_valid())
            self.assertIn('price', form.errors)
        for amount in ['0', '700.50']:
            form = EventCandidateAdminForm(data={**model_to_dict(candidate), 'price_status':'paid', 'price':amount}, instance=candidate)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.save().price_status, 'paid')

    def test_description_uses_explicit_price_status(self):
        paid = self.candidate(price=Decimal('0.00'), price_status='paid')
        self.assertIn('Ціна: 0.00', build_candidate_description(paid))
        self.assertNotIn('безкоштовний', build_candidate_description(paid))
        free = self.candidate(price=Decimal('700'), price_status='free')
        self.assertIn('безкоштовний', build_candidate_description(free))

    def test_normalization_and_staged_queries_preserve_facts(self):
        candidate = self.candidate(address='  , вул.Малечковичі   ; ', venue_name='Драмтеатр ім.Марії Заньковецької')
        self.assertEqual(normalize_location_text(candidate.address), 'вул. Малечковичі')
        self.assertEqual(location_queries(candidate), [
            'вул. Малечковичі, Львів, Україна',
            'Драмтеатр ім. Марії Заньковецької, вул. Малечковичі, Львів, Україна',
            'Драмтеатр ім. Марії Заньковецької, Львів, Україна',
            'театр імені Марії Заньковецької, Львів, Україна'])
        self.assertEqual(candidate.address, '  , вул.Малечковичі   ; ')

    def test_complete_address_success_stops_after_first_search(self):
        candidate = self.candidate(latitude=None, longitude=None, address='вул. Лесі Українки, 1')
        provider = Mock()
        provider.geocode.return_value = GeocodingResult(49.84, 24.03)
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_called_once_with('вул. Лесі Українки, 1, Львів, Україна')
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, 49.84)
        self.assertIn('[Location] geocoded', candidate.extraction_notes)
        self.assertIn('data-latitude="49.84"', EventCandidateAdmin(EventCandidate, admin.site).location_picker(candidate))

    def test_combined_and_venue_fallbacks(self):
        for position in (1, 2):
            candidate = self.candidate(latitude=None, longitude=None, address='вул.Малечковичі', venue_name=f'Example venue {position}')
            provider = Mock()
            provider.geocode.side_effect = [None] * position + [GeocodingResult(49.84, 24.03)]
            resolve_candidate_location(candidate, self.reviewer, provider)
            queries = [call.args[0] for call in provider.geocode.call_args_list]
            self.assertEqual(queries, location_queries(candidate)[:position+1])
            candidate.refresh_from_db()
            self.assertEqual(candidate.longitude, 24.03)
            self.assertEqual(candidate.address, 'вул.Малечковичі')

    def test_malformed_address_uses_venue_without_fabricating_address(self):
        candidate = self.candidate(latitude=None, longitude=None, address='??? |', venue_name='Театр')
        provider = Mock()
        provider.geocode.return_value = GeocodingResult(49.84, 24.03)
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_called_once_with('Театр, Львів, Україна')
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, '??? |')

    def test_verified_venue_reuse_and_conflicts(self):
        known = self.candidate(venue_name='Театр')
        event, _ = approve_candidate(known, self.reviewer)
        candidate = self.candidate(venue_name='театр', latitude=None, longitude=None)
        provider = Mock()
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, known.latitude)
        self.assertIn('known_venue', candidate.extraction_notes)
        self.assertIsNone(known_venue_location(self.candidate(venue_name='Інший театр', latitude=None, longitude=None)))
        event.pk = None
        from django.contrib.gis.geos import Point
        event.location = Point(24.10, 49.88, srid=4326)
        event.save()
        self.assertIsNone(known_venue_location(candidate))

    def test_previous_venue_geocode_can_be_reused(self):
        self.candidate(venue_name='Театр', address='', extraction_notes='[Location] geocoded: from candidate venue search context; address still requires review.')
        candidate = self.candidate(venue_name='Театр', address='', latitude=None, longitude=None)
        provider = Mock()
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_not_called()
        candidate.refresh_from_db()
        self.assertIn('known_venue', candidate.extraction_notes)

    def test_manual_override_does_not_reuse_old_geocoding_provenance(self):
        self.candidate(venue_name='Театр', address='', extraction_notes='[Location] geocoded: from candidate venue search context.\n[Location] Moderator selected or edited coordinates.')
        candidate = self.candidate(venue_name='Театр', address='', latitude=None, longitude=None)
        self.assertIsNone(known_venue_location(candidate))

    def test_ambiguity_stops_fallback_and_outside_bounds_does_not_set_coordinates(self):
        candidate = self.candidate(latitude=None, longitude=None, address='вул. Лесі Українки, 1')
        provider = Mock()
        provider.geocode.side_effect = GeocodingError('ambiguous')
        resolve_candidate_location(candidate, self.reviewer, provider)
        provider.geocode.assert_called_once()
        candidate.refresh_from_db()
        self.assertIsNone(candidate.latitude)
        provider.geocode.side_effect = None
        provider.geocode.return_value = GeocodingResult(50.45, 30.52)
        resolve_candidate_location(candidate, self.reviewer, provider)
        candidate.refresh_from_db()
        self.assertIsNone(candidate.latitude)
        self.assertIn('outside supported', candidate.extraction_notes)

    def test_admin_retry_current_facts_manual_coordinates_win_and_get_has_no_network(self):
        candidate = self.candidate(latitude=None, longitude=None, address='вул. Лесі Українки, 1')
        request = RequestFactory().post('/')
        request.user = self.reviewer
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        with patch.object(model_admin, 'message_user'), patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03)) as lookup:
            model_admin.resolve_location(request, EventCandidate.objects.filter(pk=candidate.pk))
        lookup.assert_called_once_with('вул. Лесі Українки, 1, Львів, Україна')
        candidate.refresh_from_db()
        candidate.latitude, candidate.longitude = 49.85, 24.04
        model_admin.save_model(request, candidate, Mock(changed_data=['latitude','longitude']), True)
        with patch('ingestion.geocoding.NominatimProvider.geocode') as lookup:
            resolve_candidate_location(candidate, self.reviewer)
            self.client.force_login(self.reviewer)
            for _ in range(2):
                self.assertEqual(self.client.get(reverse('admin:ingestion_eventcandidate_change', args=[candidate.pk])).status_code, 200)
        lookup.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, 49.85)
