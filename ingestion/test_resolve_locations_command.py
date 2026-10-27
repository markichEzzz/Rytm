from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command, CommandError
from django.forms.models import model_to_dict
from django.test import TestCase

from . import tests as fixtures
from .admin import EventCandidateAdmin
from .geocoding import GeocodingError, GeocodingResult
from .models import EventCandidate, LocationLookup, RawSourceItem, SourceAccount
from . import test_location_price as location_fixtures


class ResolveLocationsCommandTests(TestCase):
    setUp = fixtures.IngestionTests.setUp
    candidate = fixtures.IngestionTests.candidate

    def run_command(self, *args):
        output = StringIO()
        call_command('resolve_candidate_locations', *args, stdout=output)
        return output.getvalue()

    def missing(self, **kwargs):
        return self.candidate(**{'latitude': None, 'longitude': None, 'address': '', **kwargs})

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_venue_resolves_and_admin_marker_uses_saved_coordinates(self, geocode):
        candidate = self.missing()
        output = self.run_command('--id', str(candidate.pk))
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))
        self.assertEqual(candidate.address, '')
        self.assertIn('[Location] geocoded', candidate.extraction_notes)
        self.assertIn('Resolved: 1', output)
        self.assertIsNone(candidate.event_id)
        self.assertEqual(candidate.status, 'new')
        html = EventCandidateAdmin(EventCandidate, admin.site).location_picker(candidate)
        self.assertIn('data-latitude="49.84"', html)
        self.assertIn('data-longitude="24.03"', html)

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_address_resolves(self, geocode):
        candidate = self.missing(venue_name='', address='вул. Лесі Українки, 1')
        self.run_command('--id', str(candidate.pk))
        geocode.assert_called_once_with('вул. Лесі Українки, 1, Львів, Україна')
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, 49.84)

    @patch('ingestion.geocoding.NominatimProvider.geocode')
    def test_no_evidence_skips(self, geocode):
        self.missing(venue_name='')
        self.assertIn('Skipped no evidence: 1', self.run_command())
        geocode.assert_not_called()

    @patch('ingestion.geocoding.NominatimProvider.geocode')
    def test_manual_coordinates_skip_including_partial(self, geocode):
        for longitude in (24.02, None):
            candidate = self.candidate(longitude=longitude, extraction_notes='[Location] Moderator selected or edited coordinates.')
            before = model_to_dict(candidate)
            self.assertIn('Already had coordinates: 1', self.run_command('--id', str(candidate.pk)))
            candidate.refresh_from_db()
            self.assertEqual(model_to_dict(candidate), before)
        geocode.assert_not_called()

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_force_overwrites_automatic_and_explicitly_manual_coordinates(self, geocode):
        for note in ('[Location] geocoded: from candidate address.', '[Location] Moderator selected or edited coordinates.'):
            candidate = self.candidate(extraction_notes=note)
            self.run_command('--id', str(candidate.pk), '--force')
            candidate.refresh_from_db()
            self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))

    @patch('ingestion.geocoding.NominatimProvider.geocode', side_effect=GeocodingError('not_found'))
    def test_failed_force_preserves_coordinates(self, geocode):
        candidate = self.candidate()
        before = (candidate.latitude, candidate.longitude)
        self.run_command('--id', str(candidate.pk), '--force')
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), before)

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_partial_nonmanual_location_can_be_completed(self, geocode):
        candidate = self.candidate(longitude=None)
        self.run_command('--id', str(candidate.pk))
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))

    @patch('ingestion.geocoding.time.sleep')
    @patch('ingestion.geocoding.requests.get')
    def test_failed_dry_run_and_force_preview_preserve_all_rows(self, get, sleep):
        get.return_value = location_fixtures.LocationPriceTests.response(self, [])
        candidate = self.candidate()
        before = list(EventCandidate.objects.values())
        self.assertIn('Failed: 1', self.run_command('--id', str(candidate.pk), '--force', '--dry-run'))
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.assertFalse(LocationLookup.objects.exists())

    @patch('ingestion.geocoding.time.sleep')
    @patch('ingestion.geocoding.requests.get')
    def test_dry_run_changes_no_rows_and_caches_only_in_memory(self, get, sleep):
        get.return_value = location_fixtures.LocationPriceTests.response(self, [{'lat': '49.84', 'lon': '24.03', 'display_name': 'Test venue'}])
        first = self.missing()
        self.missing()
        before = list(EventCandidate.objects.values())
        lookups = list(LocationLookup.objects.values())
        self.assertIn('Resolved: 2', self.run_command('--dry-run'))
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.assertEqual(list(LocationLookup.objects.values()), lookups)
        get.assert_called_once()
        sleep.assert_called_with(1.1)
        self.assertIn('Latitude: 49.84', self.run_command('--dry-run', '--id', str(first.pk)))

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_source_filter(self, geocode):
        selected = self.missing()
        source = SourceAccount.objects.create(username='other')
        raw = RawSourceItem.objects.create(source_account=source, external_id='other', source_url='https://example.com/other')
        other = self.missing(raw_source_item=raw)
        self.assertIn('Processed: 1', self.run_command('--source', '@FESTREPUBLIC'))
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(selected.latitude)
        self.assertIsNone(other.latitude)

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_id_and_limit_filters(self, geocode):
        first, second = self.missing(), self.missing()
        self.assertIn('Processed: 1', self.run_command('--id', str(second.pk)))
        first.refresh_from_db()
        self.assertIsNone(first.latitude)
        self.assertIn('Processed: 1', self.run_command('--limit', '1', '--dry-run'))

    @patch('ingestion.geocoding.NominatimProvider.geocode', side_effect=GeocodingError('ambiguous'))
    def test_ambiguous_does_not_save_coordinates(self, geocode):
        candidate = self.missing()
        self.assertIn('Ambiguous: 1', self.run_command())
        candidate.refresh_from_db()
        self.assertIsNone(candidate.latitude)
        geocode.assert_called_once()

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(50.45, 30.52))
    def test_outside_bounds_does_not_save_coordinates(self, geocode):
        candidate = self.missing()
        self.assertIn('Failed: 1', self.run_command())
        candidate.refresh_from_db()
        self.assertIsNone(candidate.latitude)
        self.assertIn('outside supported Lviv bounds', candidate.extraction_notes)

    @patch('ingestion.geocoding.NominatimProvider.geocode')
    def test_known_venue_reuses_existing_resolver(self, geocode):
        self.candidate(address='вул. Лесі Українки, 1', extraction_notes='[Location] geocoded: from candidate venue search context.\n[Address] geocoded: reliable road/house-number address.')
        candidate = self.missing()
        self.assertIn('Source: known_venue', self.run_command('--id', str(candidate.pk)))
        geocode.assert_not_called()

    @patch('ingestion.geocoding.NominatimProvider.geocode')
    def test_published_candidates_and_invalid_options(self, geocode):
        candidate = self.missing(status='approved')
        self.assertIn('Processed: 0', self.run_command('--force'))
        for args in [('--id', str(candidate.pk)), ('--id', '999999'), ('--id', '0'), ('--limit', '0')]:
            with self.assertRaises(CommandError):
                self.run_command(*args)
        geocode.assert_not_called()
