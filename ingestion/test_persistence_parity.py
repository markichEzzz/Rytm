from copy import deepcopy
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase

from . import test_multi_extraction as fixtures
from .extraction import extract_raw_source_item
from .extractors.openai import ProviderUnavailable
from .models import EventCandidate, EventSourceObservation, RawSourceItem, SourceAccount
from .test_hybrid import response


class PersistenceParityTests(TestCase):
    setUp = fixtures.MultiExtractionTests.setUp
    set_events = fixtures.MultiExtractionTests.set_events
    extract = fixtures.MultiExtractionTests.extract

    def second_source(self, *, minutes=0):
        first = self.raw
        source = SourceAccount.objects.create(platform='telegram', username='kudypity_lviv')
        self.raw = RawSourceItem.objects.create(source_account=source, external_id='132',
            source_url='https://t.me/kudypity_lviv/132', published_at=first.published_at,
            raw_text=first.raw_text, raw_metadata={'image_text': 'Poster reference'})
        if minutes:
            self.raw.raw_text = self.raw.raw_text.replace('19:00', f'19:{minutes:02d}')
            self.raw.save()
            entry = self.mapping['events'][0]
            from datetime import datetime
            start = datetime.fromisoformat(entry['start_time']) + timedelta(minutes=minutes)
            entry['start_time'] = start.isoformat()
            entry['evidence']['start_time'][0]['quote'] = entry['evidence']['start_time'][0]['quote'].replace('19:00', f'19:{minutes:02d}')
        self.transport.return_value = response(self.mapping)

    def command(self, **options):
        output = StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider', return_value=self.provider):
            call_command('extract_events',item_id=self.raw.pk,stdout=output,**options)
        return output.getvalue()

    def test_dry_ready_event_persists_with_same_create_decision(self):
        self.set_events(1)
        before = deepcopy(self.raw.raw_metadata)
        preview = self.extract(dry_run=True)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata,before)
        self.assertFalse(EventCandidate.objects.exists())
        saved = self.extract()
        self.assertEqual((preview.ready,saved.ready),(1,1))
        self.assertEqual(preview.decisions[0]['decision'],saved.decisions[0]['decision'])
        self.assertEqual(saved.action,'created')
        self.assertEqual(EventCandidate.objects.count(),1)
        self.assertEqual(EventSourceObservation.objects.count(),1)
        self.assertTrue(saved.candidate.source_fingerprint)

    def test_confident_duplicate_links_and_has_dedicated_counter(self):
        self.set_events(1)
        canonical = self.extract().candidate
        self.second_source()
        preview = self.extract(dry_run=True)
        self.assertEqual(preview.ready,1)
        self.assertEqual(preview.linked,1)
        self.assertEqual(preview.decisions[0]['decision'],'link')
        output = self.command()
        self.assertIn('Duplicates linked: 1',output)
        self.assertIn('Manual review without candidate: 0',output)
        self.assertIn(f'Linked — ingestion.EventCandidate #{canonical.pk}',output)
        self.assertEqual(EventCandidate.objects.count(),1)
        observation = EventSourceObservation.objects.get(raw_source_item=self.raw)
        self.assertEqual(observation.candidate,canonical)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status,'extracted')

    def test_ambiguous_duplicate_is_review_in_both_modes_with_stored_reason(self):
        self.set_events(1)
        canonical = self.extract().candidate
        self.second_source(minutes=15)
        preview = self.extract(dry_run=True)
        self.assertEqual((preview.action,preview.ready),('review',0))
        self.assertEqual(preview.decisions[0]['decision'],'review')
        self.assertIn(str(canonical.pk),preview.unresolved[0]['reasons']['duplicate'][0])
        output = self.command()
        self.assertIn('Not persisted',output)
        self.assertIn('duplicate: Ambiguous/probable duplicate',output)
        self.assertIn('Manual review without candidate: 1',output)
        self.assertEqual(EventCandidate.objects.count(),1)
        self.assertFalse(EventSourceObservation.objects.filter(raw_source_item=self.raw).exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status,'new')
        audit = self.raw.raw_metadata['event_extraction']
        self.assertEqual(audit['decisions'][0]['decision'],preview.decisions[0]['decision'])
        self.assertIn('duplicate',audit['unresolved'][0]['reasons'])

    def test_model_validation_failure_reports_field_in_both_modes(self):
        self.set_events(1)
        self.raw.source_url='invalid URL'
        self.raw.save()
        preview = self.extract(dry_run=True)
        self.assertEqual(preview.ready,0)
        self.assertIn('source_url',preview.unresolved[0]['reasons'])
        output = self.command()
        self.assertIn('source_url:',output)
        self.assertIn('Not persisted',output)
        self.assertFalse(EventCandidate.objects.exists())

    def test_provider_fallback_never_creates_a_blank_candidate(self):
        self.raw.source_account.platform='telegram'
        self.raw.source_account.save()
        self.raw.raw_text = ('Комедія "Крик любові" 24.09.2026 19:00\n'
            'нова дата: 18.11.2026 19:00 (було 24.09.2026 19:00)\nвід 300 грн')
        self.raw.save()
        self.provider = Mock(extract=Mock(side_effect=ProviderUnavailable('provider_timeout')))
        preview = self.extract(dry_run=True)
        self.assertEqual((preview.action,preview.ready),('review',0))
        self.assertIn('title',preview.unresolved[0]['reasons'])
        self.assertIn('start_time',preview.unresolved[0]['reasons'])
        output = self.command()
        self.assertIn('Provider fallback: provider_timeout',output)
        self.assertIn('title: Missing or unsupported title.',output)
        self.assertIn('start_time: Missing, ambiguous, or unsupported start time.',output)
        self.assertFalse(EventCandidate.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status,'new')
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'],'provider_timeout')

    def test_existing_candidate_rerun_preserves_moderator_values(self):
        self.set_events(1)
        candidate = self.extract().candidate
        candidate.title='Moderator title'
        candidate.save()
        output=self.command(reextract=True,dry_run=True)
        self.assertIn('Preserved existing',output)
        self.command(reextract=True)
        self.assertEqual(EventCandidate.objects.count(),1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title,'Moderator title')

    def test_existing_incomplete_legacy_candidate_without_reextract_is_preserved(self):
        candidate=EventCandidate.objects.create(raw_source_item=self.raw,title='',start_time=None)
        output=self.command()
        self.assertIn('use --reextract to retry incomplete fields',output)
        self.assertIn(f'Candidate #{candidate.pk}',output)
        self.assertEqual(EventCandidate.objects.count(),1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title,'')
        self.assertIsNone(candidate.start_time)
        self.transport.assert_not_called()

    def test_single_result_and_batch_share_missing_field_validation(self):
        from .extractors.contracts import ExtractionBatch,ExtractionResult
        from .extraction import store_batch
        event=ExtractionResult(True,price=None,category='performance')
        self.provider=Mock(extract=Mock(side_effect=ProviderUnavailable('provider_timeout')))
        self.raw.raw_text='Вистава 24.09.2026 19:00 та 18.11.2026 19:00'
        single=extract_raw_source_item(self.raw,provider=self.provider,dry_run=True)
        batch=store_batch(self.raw,ExtractionBatch(True,events=(event,)),['new'],dry_run=True)
        self.assertEqual((single.ready,batch.ready),(0,0))
        self.assertEqual(set(single.unresolved[0]['reasons']),set(batch.unresolved[0]['reasons']))

    def test_database_constraint_failure_is_sanitized_and_stored(self):
        from django.db import IntegrityError
        from types import SimpleNamespace
        self.set_events(1)
        cause=Exception('SECRET database SQL')
        cause.diag=SimpleNamespace(constraint_name='unique_source_event_observation')
        failure=IntegrityError('SECRET database SQL')
        failure.__cause__=cause
        with patch('ingestion.deduplication.store_observation',side_effect=failure):
            outcome=self.extract()
        self.assertEqual(outcome.action,'failed')
        self.assertIn('source_observation',outcome.unresolved[0]['reasons'])
        self.assertNotIn('SECRET',outcome.error)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.extraction_status,'failed')
        self.assertEqual(self.raw.raw_metadata['event_extraction']['candidate_ready'],0)
        self.assertIn('source_observation',self.raw.raw_metadata['event_extraction']['persistence_error']['reasons'])
        self.assertFalse(EventCandidate.objects.exists())

    def test_concurrent_status_change_is_explained_without_writes(self):
        self.set_events(1)
        from .extraction import store_batch
        from .extractors.contracts import ExtractionBatch,ExtractionResult,KYIV
        from datetime import datetime
        RawSourceItem.objects.filter(pk=self.raw.pk).update(extraction_status='ignored')
        event=ExtractionResult(True,title='Title',start_time=datetime(2026,10,12,19,tzinfo=KYIV))
        outcome=store_batch(self.raw,ExtractionBatch(True,events=(event,)),['new'],dry_run=False)
        self.assertEqual(outcome.action,'skipped')
        self.assertEqual(outcome.ready,0)
        self.assertIn('changed during processing',outcome.decisions[0]['reason'])
        self.assertFalse(EventCandidate.objects.exists())
