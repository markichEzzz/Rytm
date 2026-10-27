from copy import deepcopy
from unittest.mock import patch

from django.contrib import admin
from django.test import TestCase

from . import test_multi_extraction as fixtures
from .admin import EventCandidateAdmin
from .extraction import extract_raw_source_item
from .extractors.hybrid import batch_mapping
from .extractors.contracts import ExtractionBatch
from .extractors.location_evidence import location_supported
from .geocoding import GeocodingResult
from .models import EventCandidate
from .test_hybrid import response


class LocationExtractionTests(TestCase):
    setUp = fixtures.MultiExtractionTests.setUp
    set_events = fixtures.MultiExtractionTests.set_events
    extract = fixtures.MultiExtractionTests.extract

    def shared_post(self, header='Події жовтня у !FESTrepublic', count=2):
        self.set_events(count)
        self.raw.raw_text = header + '\n\n' + '\n\n'.join(
            '\n'.join(line for line in text.splitlines() if not line.startswith('Місце:'))
            for _, text in [fixtures.event(i) for i in range(count)])
        self.raw.save()
        for entry in self.mapping['events']:
            entry['venue_name'] = None
            entry['evidence']['venue_name'] = []
        self.transport.return_value = response(self.mapping)

    def test_shared_caption_header_inherits_and_records_private_provenance(self):
        self.shared_post('Ділимось підбіркою майбутніх подій у жовтні на яких зустрінемось із вами на !FESTrepublic')
        outcome = self.extract()
        self.assertEqual({candidate.venue_name for candidate in outcome.candidates}, {'FESTrepublic'})
        self.assertEqual({event.location_provenance['venue_name'] for event in outcome.results}, {'shared_post_context'})
        self.assertTrue(all(candidate.address == '' for candidate in outcome.candidates))
        self.assertTrue(all('shared_post_context' in candidate.extraction_notes for candidate in outcome.candidates))
        self.raw.refresh_from_db()
        self.assertEqual(ExtractionBatch.from_mapping(self.raw.raw_metadata['event_extraction']['batch']).shared_context['venue_name'], 'FESTrepublic')

    def test_llm_shared_context_is_grounded_and_individual_venue_overrides(self):
        self.shared_post()
        self.raw.raw_text += '\nМісце: Драмтеатр ім. Марії Заньковецької'
        self.raw.save()
        self.mapping['events'][1]['venue_name'] = 'театр імені Марії Заньковецької'
        self.mapping['events'][1]['evidence']['venue_name'] = [{'source': 'caption', 'quote': 'Місце: Драмтеатр ім. Марії Заньковецької'}]
        self.mapping['shared_context'] = {'venue_name': 'FESTrepublic', 'address': None, 'evidence': {
            'venue_name': [{'source': 'caption', 'quote': 'Події жовтня у !FESTrepublic'}], 'address': []}}
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual([candidate.venue_name for candidate in outcome.candidates], ['FESTrepublic', 'театр імені Марії Заньковецької'])
        self.assertEqual(outcome.results[1].location_provenance['venue_name'], 'event_specific')

    def test_source_account_without_independent_evidence_never_becomes_venue(self):
        self.shared_post(header='Жовтневі події')
        self.mapping['shared_context'] = {'venue_name': 'FESTrepublic', 'address': None, 'evidence': {
            'venue_name': [{'source': 'caption', 'quote': 'Події у FESTrepublic'}], 'address': []}}
        self.transport.return_value = response(self.mapping)
        self.assertTrue(all(candidate.venue_name == '' for candidate in self.extract().candidates))

    def test_location_alias_punctuation_inflection_and_mixed_script_noise(self):
        self.assertTrue(location_supported('FESTrepublic', '!FESTrepublic'))
        self.assertTrue(location_supported('театр імені Марії Заньковецької', 'Драмтеатр ім. Марії Заньковецької'))
        self.assertTrue(location_supported('театр імені Марії Заньковецької', 'У театрі імені Марії Заньковецької'))
        self.assertTrue(location_supported('Драмтеатр ім. Марії Заньковецької', 'Драмтеатр ім. Мaрії Заньковецької', ocr=True))
        self.assertFalse(location_supported('Зал 1', 'Зал 2'))
        self.assertFalse(location_supported('FESTrepublic', 'FEST republican'))

    def test_hallucinated_venue_with_real_quote_is_removed(self):
        self.set_events(1)
        self.mapping['events'][0]['venue_name'] = 'Invented venue'
        self.transport.return_value = response(self.mapping)
        self.assertEqual(self.extract().candidates[0].venue_name, '')

    def test_explicit_street_without_house_number_is_extracted(self):
        self.shared_post(header='Події жовтня')
        self.raw.raw_text += '\nМісце: Malevich Concert Arena, проспект Чорновола'
        self.raw.save()
        outcome = self.extract()
        self.assertEqual(outcome.candidates[1].venue_name, 'Malevich Concert Arena')
        self.assertEqual(outcome.candidates[1].address, 'проспект Чорновола')
        self.assertEqual(outcome.candidates[0].address, '')

    def test_ocr_only_unscoped_logo_remains_uncertain(self):
        self.shared_post(header='Події жовтня')
        self.raw.raw_metadata['image_text'] = 'FESTrepublic'
        self.raw.save()
        self.mapping['events'][0]['venue_name'] = 'FESTrepublic'
        self.mapping['events'][0]['evidence']['venue_name'] = [{'source': 'ocr', 'quote': 'FESTrepublic'}]
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(outcome.candidates[0].venue_name, '')
        self.assertEqual(outcome.results[0].location_provenance['venue_name'], 'ocr_only_uncertain')

    def test_conflicting_program_headers_do_not_apply_one_venue_globally(self):
        self.shared_post()
        self.raw.raw_text += '\n\nПодії листопада у Іншому театрі'
        self.raw.save()
        self.assertTrue(all(candidate.venue_name == '' for candidate in self.extract().candidates))

    def test_reextract_enriches_missing_fields_and_preserves_identity_and_manual_edits(self):
        self.shared_post(header='Жовтневі події')
        outcome = self.extract()
        first, second = outcome.candidates
        original = first.source_fingerprint
        second.venue_name = 'Moderator venue'
        second.save()
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.transport.return_value = response(self.mapping)
        result = self.extract(reextract=True)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result.enriched, 1)
        self.assertEqual(first.venue_name, 'FESTrepublic')
        self.assertEqual(first.source_fingerprint, original)
        self.assertEqual(second.venue_name, 'Moderator venue')
        self.assertEqual(EventCandidate.objects.count(), 2)

    def test_moderator_cleared_location_is_protected(self):
        self.shared_post(header='Жовтневі події')
        candidate = self.extract().candidates[0]
        candidate.extraction_notes += '\n[Location facts] Moderator edited: venue_name'
        candidate.save()
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.venue_name, '')

    def test_historical_admin_clear_is_protected_by_change_log(self):
        import json
        from django.contrib.admin.models import LogEntry, CHANGE
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        self.shared_post(header='Жовтневі події')
        candidate = self.extract().candidates[0]
        reviewer = get_user_model().objects.create_user(username='location-reviewer')
        LogEntry.objects.create(user=reviewer, content_type=ContentType.objects.get_for_model(EventCandidate),
            object_id=str(candidate.pk), object_repr=candidate.title, action_flag=CHANGE,
            change_message=json.dumps([{'changed': {'fields': ['Venue name']}}]))
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.venue_name, '')

    def test_admin_venue_edit_invalidates_old_geocoding_reuse_without_changing_coordinates(self):
        from types import SimpleNamespace
        from django.test import RequestFactory
        from .geocoding import known_venue_location
        self.shared_post(count=1)
        candidate = self.extract().candidates[0]
        candidate.latitude, candidate.longitude = 49.84, 24.03
        candidate.extraction_notes = '[Location] geocoded: from candidate venue search context.'
        candidate.venue_name = 'Different venue'
        EventCandidateAdmin(EventCandidate, admin.site).save_model(RequestFactory().post('/'), candidate,
            SimpleNamespace(changed_data=['venue_name']), change=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))
        self.assertIsNone(known_venue_location(EventCandidate(raw_source_item=self.raw, venue_name='Different venue')))

    @patch('ingestion.geocoding.NominatimProvider.geocode', return_value=GeocodingResult(49.84, 24.03))
    def test_resolver_receives_new_venue_and_coordinates_appear_on_admin_map(self, geocode):
        self.shared_post(header='Жовтневі події')
        candidate = self.extract().candidates[0]
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.extract(reextract=True, resolve_locations=True)
        candidate.refresh_from_db()
        self.assertEqual((candidate.latitude, candidate.longitude), (49.84, 24.03))
        geocode.assert_called_with('FESTrepublic, Львів, Україна')
        self.assertIn('data-latitude="49.84"', EventCandidateAdmin(EventCandidate, admin.site).location_picker(candidate))

    @patch('ingestion.geocoding.NominatimProvider.geocode')
    def test_manual_coordinates_win_after_enrichment(self, geocode):
        self.shared_post(header='Жовтневі події', count=1)
        candidate = self.extract().candidates[0]
        candidate.latitude, candidate.longitude = 49.85, 24.05
        candidate.save()
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.extract(reextract=True, resolve_locations=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.latitude, 49.85)
        geocode.assert_not_called()

    def test_reextract_dry_run_preserves_database_and_reports_enrichment(self):
        self.shared_post(header='Жовтневі події')
        self.extract()
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        before = list(EventCandidate.objects.values())
        metadata = deepcopy(self.raw.raw_metadata)
        self.assertEqual(self.extract(reextract=True, dry_run=True).enriched, 2)
        self.assertEqual(list(EventCandidate.objects.values()), before)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata, metadata)

    def test_invalid_event_schema_retains_grounded_location_for_private_review(self):
        self.shared_post()
        self.mapping['events'][0]['price'] = '-20'
        self.transport.return_value = response(self.mapping)
        outcome = self.extract(dry_run=True)
        review = next(entry for entry in outcome.unresolved if entry['title'] == 'Гурт 0')
        self.assertEqual(review['venue_name'], 'FESTrepublic')
        self.assertEqual(review['location_provenance']['venue_name'], 'shared_post_context')
        self.assertEqual(outcome.ready, 1)
        self.assertFalse(EventCandidate.objects.exists())

    def test_missing_new_date_can_enrich_a_unique_existing_source_entry(self):
        self.shared_post(header='Жовтневі події')
        candidate = self.extract().candidates[0]
        original_start = candidate.start_time
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.mapping['events'][0]['start_time'] = None
        self.transport.return_value = response(self.mapping)
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.venue_name, 'FESTrepublic')
        self.assertEqual(candidate.start_time, original_start)
        self.assertEqual(EventCandidate.objects.count(), 2)

    def test_invalid_price_does_not_block_location_enrichment_of_existing_entry(self):
        self.shared_post(header='Жовтневі події')
        candidate = self.extract().candidates[0]
        self.raw.refresh_from_db()
        self.raw.raw_text = 'Події жовтня у FESTrepublic\n\n' + self.raw.raw_text
        self.raw.save()
        self.mapping['events'][0]['price'] = '-20'
        self.transport.return_value = response(self.mapping)
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.venue_name, 'FESTrepublic')
        self.assertIsNone(candidate.price)
        self.assertEqual(EventCandidate.objects.count(), 2)

    def test_private_location_provenance_is_not_public_serializer_field(self):
        from events.serializers import EventSerializer
        self.assertNotIn('location_provenance', EventSerializer().fields)
        self.assertNotIn('shared_context', EventSerializer().fields)
        self.shared_post()
        self.extract()
        self.assertNotIn('shared_post_context', self.client.get('/api/events/').content.decode())
