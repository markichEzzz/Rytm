from copy import deepcopy
from datetime import datetime
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase
from django.db import IntegrityError, transaction

from events.models import Event
from .admin import RawSourceItemAdmin, EventCandidateAdmin
from .extraction import extract_raw_source_item
from .extractors.contracts import EVIDENCE_FIELDS, KYIV, ExtractionResult
from .extractors.hybrid import result_mapping
from .extractors.openai import OpenAIEventExtractionProvider
from .models import SourceAccount, RawSourceItem, EventCandidate
from .test_hybrid import response

NOW = datetime(2026, 10, 7, 12, tzinfo=KYIV)


def event(index, venue=None):
    title = f'Гурт {index}'
    day = 8 + index
    venue = venue or f'Зал {index}'
    text = f'Концерт: {title}\n{day} жовтня 19:00\nМісце: {venue}'
    evidence = {name: [] for name in EVIDENCE_FIELDS}
    for key, quote in {'title': title, 'start_time': f'{day} жовтня 19:00',
                        'venue_name': f'Місце: {venue}', 'category': f'Концерт: {title}'}.items():
        evidence[key] = [{'source': 'caption', 'quote': quote}]
    mapping = result_mapping(ExtractionResult(True, title=title,
        start_time=datetime(2026, 10, day, 19, tzinfo=KYIV), venue_name=venue,
        category='concert', confidence=.9, evidence=evidence, needs_review=False))
    return {k: v for k, v in mapping.items() if k not in ('is_event', 'event_count', 'extraction_notes')}, text


class MultiExtractionTests(TestCase):
    def setUp(self):
        self.source = SourceAccount.objects.create(username='festrepublic')
        self.raw = RawSourceItem.objects.create(source_account=self.source, external_id='schedule',
            source_url='https://www.instagram.com/p/schedule/', published_at=NOW,
            raw_image_url='https://example.com/PRIVATE_IMAGE.jpg', raw_metadata={'image_text': 'Poster reference'})
        self.transport = Mock()
        self.provider = OpenAIEventExtractionProvider(api_key='FAKE_TEST_KEY', model='configured-test', transport=self.transport)
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('Tests cannot use network'))
        network.start()
        self.addCleanup(network.stop)

    def set_events(self, count=2):
        pairs = [event(i) for i in range(count)]
        self.raw.raw_text = '\n\n'.join(text for _, text in pairs)
        self.raw.save()
        self.mapping = {'is_event': True, 'events': [entry for entry, _ in pairs],
                        'overall_notes': 'Calendar; each event needs review.', 'needs_review': True}
        self.transport.return_value = response(self.mapping)

    def extract(self, **kwargs):
        return extract_raw_source_item(self.raw, provider=self.provider, **kwargs)

    def test_one_raw_many_individual_candidates(self):
        self.set_events()
        outcome = self.extract()
        self.assertEqual(outcome.ready, 2)
        self.assertEqual(len(outcome.candidates), 2)
        self.assertEqual(EventCandidate.objects.count(), 2)
        self.assertEqual({c.source_account for c in outcome.candidates}, {self.source})
        for candidate in outcome.candidates:
            self.assertEqual(candidate.source_url, self.raw.source_url)
            self.assertEqual(candidate.raw_source_item_id, self.raw.pk)
            self.assertEqual(candidate.status, 'new')
            self.assertTrue(candidate.source_fingerprint)
            self.assertFalse(candidate.public_photo)
            self.assertIsNone(candidate.price)
            self.assertNotEqual(candidate.description, self.raw.raw_text)
        self.assertNotEqual(outcome.candidates[0].description, outcome.candidates[1].description)
        self.assertFalse(Event.objects.exists())
        public = self.client.get('/api/events/')
        self.assertEqual(public.status_code, 200)
        self.assertNotIn('Гурт', public.content.decode())
        self.assertNotIn('PRIVATE_IMAGE', public.content.decode())

    def test_seventeen_entries(self):
        self.set_events(17)
        outcome = self.extract()
        self.assertEqual(outcome.detected, 17)
        self.assertEqual(outcome.ready, 17)
        self.assertEqual(len(outcome.candidates), 17)
        self.assertFalse(outcome.unresolved)

    def test_single_array_event_still_works(self):
        self.set_events(1)
        self.assertEqual(len(self.extract().candidates), 1)

    def test_bad_event_does_not_discard_siblings(self):
        self.set_events(4)
        self.mapping['events'][1]['price'] = '-20'
        self.mapping['events'][2]['start_time'] = None
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(outcome.detected, 4)
        self.assertEqual(outcome.ready, 2)
        self.assertEqual(len(outcome.unresolved), 2)
        self.raw.refresh_from_db()
        self.assertEqual(len(self.raw.raw_metadata['event_extraction']['unresolved']), 2)
        self.assertEqual(EventCandidate.objects.count(), 2)

    def test_repeated_mentions_not_duplicated(self):
        self.set_events()
        self.mapping['events'].append(deepcopy(self.mapping['events'][0]))
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(len(outcome.candidates), 2)
        self.assertEqual(outcome.duplicates, 1)

    def test_duplicate_mention_without_venue_is_not_an_extra_event(self):
        self.set_events(1)
        duplicate = deepcopy(self.mapping['events'][0])
        duplicate['venue_name'] = None
        duplicate['evidence']['venue_name'] = []
        self.mapping['events'].append(duplicate)
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(outcome.ready, 1)
        self.assertEqual(outcome.duplicates, 1)
        self.assertEqual(len(outcome.candidates), 1)

    def test_command_counts_created_rows_not_raw_posts(self):
        self.set_events(3)
        output = StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=self.provider):
            call_command('extract_events', source='festrepublic', limit=1, stdout=output)
        self.assertIn('Candidates created: 3', output.getvalue())
        self.assertEqual(EventCandidate.objects.count(), 3)

    def test_default_idempotence_and_explicit_reextract(self):
        self.set_events()
        self.extract()
        self.raw.refresh_from_db()
        self.assertEqual(self.extract().action, 'skipped')
        again = self.extract(reextract=True)
        self.assertEqual(again.matched, 2)
        self.assertEqual(EventCandidate.objects.count(), 2)
        self.transport.assert_called_once()

    def test_reextract_adds_new_event_and_preserves_moderator_edits(self):
        self.set_events()
        outcome = self.extract()
        candidate = outcome.candidates[0]
        candidate.title = 'Moderator corrected'
        candidate.venue_name = 'Moderator venue'
        candidate.status = 'rejected'
        candidate.save()
        self.raw.refresh_from_db()
        self.set_events(3)
        outcome = self.extract(reextract=True)
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.matched, 2)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title, 'Moderator corrected')
        self.assertEqual(candidate.venue_name, 'Moderator venue')
        self.assertEqual(candidate.status, 'rejected')
        self.assertEqual(EventCandidate.objects.count(), 3)

    def test_reordered_events_use_facts_not_position(self):
        self.set_events(3)
        self.extract()
        self.raw.refresh_from_db()
        self.provider.model = 'new-model'
        self.mapping['events'].reverse()
        self.transport.return_value = response(self.mapping)
        outcome = self.extract(reextract=True)
        self.assertEqual(outcome.matched, 3)
        self.assertEqual(EventCandidate.objects.count(), 3)

    def test_supported_shared_venue_and_individual_override(self):
        self.set_events()
        self.raw.raw_text = 'Усі події у FESTrepublic.\n' + self.raw.raw_text
        self.raw.save()
        first = self.mapping['events'][0]
        first['venue_name'] = 'FESTrepublic'
        first['evidence']['venue_name'] = [{'source': 'caption', 'quote': 'Усі події у FESTrepublic.'}]
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(outcome.candidates[0].venue_name, 'FESTrepublic')
        self.assertEqual(outcome.candidates[1].venue_name, 'Зал 1')

    def test_account_name_is_not_venue_evidence(self):
        self.set_events(1)
        self.mapping['events'][0]['venue_name'] = 'FESTrepublic'
        self.mapping['events'][0]['evidence']['venue_name'] = [{'source': 'caption', 'quote': 'FESTrepublic'}]
        self.transport.return_value = response(self.mapping)
        self.assertEqual(self.extract().candidates[0].venue_name, '')

    def test_venue_price_and_date_from_other_entry_not_borrowed(self):
        self.set_events()
        first, second = self.mapping['events']
        first['venue_name'] = second['venue_name']
        first['evidence']['venue_name'] = second['evidence']['venue_name']
        first['start_time'] = second['start_time']
        first['evidence']['start_time'] = second['evidence']['start_time']
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(len(outcome.unresolved), 1)

    def test_unknown_category_and_clamped_confidence(self):
        self.set_events(1)
        self.mapping['events'][0]['category'] = 'not-a-category'
        self.mapping['events'][0]['confidence'] = 4
        self.transport.return_value = response(self.mapping)
        candidate = self.extract().candidates[0]
        self.assertEqual(candidate.category, '')
        self.assertLessEqual(candidate.confidence, 1)

    def test_multi_day_interval_is_one_candidate(self):
        self.set_events(1)
        self.raw.raw_text = 'Концерт: Один в каное\n20–21 жовтня 19:00–22:00'
        self.raw.save()
        entry = self.mapping['events'][0]
        entry.update(title='Один в каное', start_time='2026-10-20T19:00:00+03:00',
                     end_time='2026-10-21T22:00:00+03:00', venue_name=None)
        entry['evidence'].update(title=[{'source': 'caption', 'quote': 'Один в каное'}],
            start_time=[{'source': 'caption', 'quote': '20–21 жовтня 19:00–22:00'}],
            end_time=[{'source': 'caption', 'quote': '20–21 жовтня 19:00–22:00'}],
            venue_name=[], category=[{'source': 'caption', 'quote': 'Концерт: Один в каное'}])
        self.transport.return_value = response(self.mapping)
        outcome = self.extract()
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0].start_time.day, 20)
        self.assertEqual(outcome.candidates[0].end_time.day, 21)

    def test_admin_source_and_all_sibling_links(self):
        self.set_events()
        outcome = self.extract()
        raw_admin = RawSourceItemAdmin(RawSourceItem, admin.site)
        self.assertIn('View candidates (2)', raw_admin.candidate_link(self.raw))
        candidate_admin = EventCandidateAdmin(EventCandidate, admin.site)
        for candidate in outcome.candidates:
            self.assertIn(candidate.title, candidate_admin.sibling_candidates(outcome.candidates[0]))
        self.assertIn(self.raw.source_url, candidate_admin.source_post(outcome.candidates[0]))

    def test_database_uniqueness_defends_same_fingerprint(self):
        self.set_events(1)
        candidate = self.extract().candidates[0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventCandidate.objects.create(raw_source_item=self.raw, source_fingerprint=candidate.source_fingerprint)

    def test_readable_dry_run_does_not_write(self):
        self.set_events(3)
        self.mapping['events'][2]['start_time'] = None
        self.transport.return_value = response(self.mapping)
        before = dict(self.raw.raw_metadata)
        output = StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=self.provider):
            call_command('extract_events', source='festrepublic', limit=1, dry_run=True, stdout=output)
        self.assertIn('Detected events: 3', output.getvalue())
        self.assertIn('Candidate-ready: 2', output.getvalue())
        self.assertIn('Needs manual completion: 1', output.getvalue())
        self.assertIn('Persisted: 0 (dry-run)', output.getvalue())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata, before)
        self.assertFalse(EventCandidate.objects.exists())
