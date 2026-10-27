from datetime import timedelta
from hashlib import sha256
from io import StringIO
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from events.serializers import EventSerializer
from . import tests as fixtures
from . import test_location_price as provider_fixtures
from .addresses import canonical_address
from .admin import EventCandidateAdmin
from .geocoding import GeocodingError, GeocodingResult, NominatimProvider, enrich_candidate_location
from .models import EventCandidate, LocationLookup
from .provenance import address_source
from .services import approve_candidate
from .extraction import candidate_from_result
from .extractors.contracts import ExtractionResult


class AddressEnrichmentTests(TestCase):
    setUp = fixtures.IngestionTests.setUp
    candidate = fixtures.IngestionTests.candidate

    def missing(self, **kwargs):
        return self.candidate(**{'address': '', 'latitude': None, 'longitude': None, **kwargs})

    def provider(self, latitude=49.84, longitude=24.03):
        return Mock(geocode=Mock(return_value=GeocodingResult(latitude, longitude,
            'LONG DISPLAY NAME MUST NOT BE STORED', {'road': 'вулиця Лесі Українки', 'house_number': '1', 'city': 'Львів'})))

    def test_road_house_number_ranges_suffixes_and_missing_numbers(self):
        for road, house, expected in [
            ('вулиця Лесі Українки', '1', 'вул. Лесі Українки, 1'),
            ('Старознесенська вулиця', '24-26', 'вул. Старознесенська, 24-26'),
            ("проспект В'ячеслава Чорновола", '2', "просп. В'ячеслава Чорновола, 2"),
            ('площа Ринок', '', 'пл. Ринок'), ('провулок Кривий', None, 'пров. Кривий'),
            ('вулиця Лесі Українки', 'not a number', 'вул. Лесі Українки')]:
            self.assertEqual(canonical_address({'road': road, 'house_number': house}), expected)
        self.assertEqual(canonical_address({'display_name': 'Long geocoder string'}), '')
        self.assertEqual(canonical_address({'road': 'Львів, область, Україна'}), '')

    def test_success_fills_empty_address_and_private_provenance(self):
        candidate = self.missing()
        outcome = enrich_candidate_location(candidate, provider=self.provider())
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, 'вул. Лесі Українки, 1')
        self.assertEqual(address_source(candidate), 'geocoded')
        self.assertTrue(outcome.address_enriched)
        self.assertTrue(outcome.coordinates_enriched)
        self.assertNotIn('LONG DISPLAY', candidate.address)

    def test_extracted_address_has_explicit_private_provenance(self):
        raw = self.candidate().raw_source_item
        candidate = candidate_from_result(raw, ExtractionResult(True, address='вул. Лесі Українки, 1'))
        self.assertEqual(address_source(candidate), 'extracted')

    def test_existing_manual_address_is_preserved(self):
        candidate = self.missing(address='вул. Інша, 99', extraction_notes='[Location facts] Moderator edited: address')
        enrich_candidate_location(candidate, provider=self.provider(), force=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, 'вул. Інша, 99')
        self.assertEqual(address_source(candidate), 'moderator')

    def test_deliberately_cleared_manual_address_wins(self):
        candidate = self.missing(extraction_notes='[Location facts] Moderator edited: address')
        enrich_candidate_location(candidate, provider=self.provider(), force=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, '')
        self.assertEqual(address_source(candidate), 'moderator')

    def test_known_approved_venue_reuses_reliable_address(self):
        known = self.candidate(address='вулиця Лесі Українки, 1')
        approve_candidate(known, self.reviewer)
        candidate = self.missing()
        provider = self.provider()
        outcome = enrich_candidate_location(candidate, provider=provider, enrich_addresses=True)
        provider.geocode.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, 'вул. Лесі Українки, 1')
        self.assertEqual(outcome.address_source, 'known_venue')

    def test_known_geocoded_address_is_reused_but_unverified_pending_address_is_not(self):
        known = self.candidate(address='вул. Лесі Українки, 1', extraction_notes='[Location] geocoded: from candidate venue search context.')
        candidate = self.missing()
        provider = Mock(geocode=Mock(side_effect=GeocodingError('not_found')))
        enrich_candidate_location(candidate, provider=provider, enrich_addresses=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, '')
        self.assertTrue(provider.geocode.called)
        known.extraction_notes += '\n[Address] geocoded: reliable road/house-number address.'
        known.save()
        provider.geocode.reset_mock()
        outcome = enrich_candidate_location(candidate, provider=provider, enrich_addresses=True)
        self.assertEqual(outcome.address_source, 'known_venue')
        provider.geocode.assert_not_called()

    def test_address_only_command_preserves_existing_manual_coordinates(self):
        candidate = self.candidate(address='', extraction_notes='[Location] Moderator selected or edited coordinates.')
        before = (candidate.latitude, candidate.longitude)
        output = StringIO()
        with patch('ingestion.geocoding.NominatimProvider.geocode', return_value=self.provider().geocode.return_value):
            call_command('resolve_candidate_locations', '--id', str(candidate.pk), stdout=output)
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), before)
        self.assertEqual(candidate.address, 'вул. Лесі Українки, 1')
        self.assertIn('Coordinates enriched: 0', output.getvalue())
        self.assertIn('Addresses enriched: 1', output.getvalue())
        self.assertIn('Moderator selected or edited coordinates.', EventCandidateAdmin(EventCandidate, admin.site).location_provenance(candidate))

    def test_result_far_from_existing_manual_point_does_not_fill_address(self):
        candidate = self.candidate(address='')
        outcome = enrich_candidate_location(candidate, provider=self.provider(49.88, 24.10), enrich_addresses=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, '')
        self.assertEqual(outcome.status, 'failed')
        self.assertIn('disagrees', outcome.message)

    def test_outside_bounds_and_ambiguity_do_not_fill_address(self):
        for provider in [self.provider(50.45, 30.52), Mock(geocode=Mock(side_effect=GeocodingError('ambiguous')))]:
            candidate = self.missing()
            enrich_candidate_location(candidate, provider=provider)
            candidate.refresh_from_db()
            self.assertEqual(candidate.address, '')
            self.assertIsNone(candidate.latitude)

    def test_display_name_without_structured_road_is_not_used_as_address(self):
        candidate = self.missing()
        provider = Mock(geocode=Mock(return_value=GeocodingResult(49.84, 24.03, 'вулиця Лесі Українки, 1, Львів, Україна')))
        enrich_candidate_location(candidate, provider=provider)
        candidate.refresh_from_db()
        self.assertEqual(candidate.address, '')
        self.assertIsNotNone(candidate.latitude)

    @patch('ingestion.geocoding.time.sleep')
    @patch('ingestion.geocoding.requests.get')
    def test_dry_run_address_changes_no_candidate_or_cache_rows(self, get, sleep):
        candidate = self.candidate(address='')
        get.return_value = provider_fixtures.LocationPriceTests.response(self, [{'lat': str(candidate.latitude), 'lon': str(candidate.longitude),
            'display_name': 'Long display', 'address': {'road': 'вулиця Лесі Українки', 'house_number': '1'}}])
        before = list(EventCandidate.objects.values())
        outcome = enrich_candidate_location(candidate, dry_run=True, enrich_addresses=True)
        self.assertTrue(outcome.address_enriched)
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.assertFalse(LocationLookup.objects.exists())

    @patch('ingestion.geocoding.requests.get')
    def test_legacy_cache_upgrades_once_then_reuses_structured_components(self, get):
        query = 'Test venue, Львів, Україна'
        key = sha256((settings.GEOCODING_NOMINATIM_URL + '\n' + query.casefold()).encode()).hexdigest()
        LocationLookup.objects.create(key=key, query=query, result={'latitude':49.84, 'longitude':24.03, 'display_name':'Old'}, looked_up_at=timezone.now()-timedelta(seconds=2))
        get.return_value = provider_fixtures.LocationPriceTests.response(self, [{'lat':'49.84', 'lon':'24.03', 'address': {'road':'вулиця Лесі Українки', 'house_number':'1'}}])
        provider = NominatimProvider(require_address=True)
        self.assertEqual(provider.geocode(query).address_components['road'], 'вулиця Лесі Українки')
        provider.geocode(query)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['params']['addressdetails'], 1)

    @patch('ingestion.geocoding.requests.get')
    def test_cached_missing_road_does_not_trigger_repeated_lookups(self, get):
        get.return_value = provider_fixtures.LocationPriceTests.response(self, [{'lat':'49.84', 'lon':'24.03'}])
        provider = NominatimProvider(require_address=True)
        provider.geocode('Unknown address')
        provider.geocode('Unknown address')
        get.assert_called_once()

    def test_admin_exposes_enriched_address_as_editable_and_approval_accepts_it(self):
        candidate = self.missing(price=None, price_status='unknown')
        enrich_candidate_location(candidate, provider=self.provider())
        candidate.refresh_from_db()
        model_admin = EventCandidateAdmin(EventCandidate, admin.site)
        self.assertIn('address', model_admin.list_display)
        self.assertNotIn('address', model_admin.readonly_fields)
        self.assertEqual(model_admin.address_provenance(candidate), 'geocoded')
        event, _ = approve_candidate(candidate, self.reviewer)
        self.assertEqual(event.address, 'вул. Лесі Українки, 1')
        data = EventSerializer(event).data
        self.assertIsNone(data['price'])
        self.assertEqual(data['price_status'], 'unknown')
        self.assertNotIn('address_source', data)
