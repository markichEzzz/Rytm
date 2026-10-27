from copy import deepcopy
from datetime import datetime
from io import StringIO
from unittest.mock import Mock,patch
import json

from django.contrib.admin.models import CHANGE,LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from . import test_multi_extraction as fixtures
from .extractors.openai import ProviderUnavailable
from .models import EventCandidate,EventSourceObservation
from .test_hybrid import response


class CandidateRepairTests(TestCase):
    setUp=fixtures.MultiExtractionTests.setUp
    set_events=fixtures.MultiExtractionTests.set_events
    extract=fixtures.MultiExtractionTests.extract

    def draft(self,**overrides):
        self.set_events(1)
        self.raw.source_account.platform='telegram'
        self.raw.source_account.username='kudypity_lviv'
        self.raw.source_account.save()
        self.old_audit={'provider':'deterministic','status':'fallback','historical_marker':'keep original audit'}
        self.raw.raw_metadata['event_extraction']=deepcopy(self.old_audit)
        self.raw.extraction_status='extracted'
        self.raw.save()
        return EventCandidate.objects.create(raw_source_item=self.raw,price_status='unknown',**overrides)

    def command(self,**options):
        output=StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider',return_value=self.provider):
            call_command('extract_events',item_id=self.raw.pk,stdout=output,**options)
        return output.getvalue()

    def test_incomplete_without_reextract_is_preserved(self):
        candidate=self.draft()
        outcome=self.extract()
        self.assertEqual(outcome.action,'skipped')
        candidate.refresh_from_db()
        self.assertEqual(candidate.title,'')
        self.transport.assert_not_called()

    def test_retry_fills_missing_fields_and_keeps_identity_history_and_source_links(self):
        candidate=self.draft(extraction_notes='Original provenance')
        old=EventSourceObservation.objects.create(raw_source_item=self.raw,candidate=candidate,
            fingerprint='original-fallback',facts={'original':'keep'},match_status='probable')
        original_id=candidate.pk
        outcome=self.extract(reextract=True)
        self.assertEqual((outcome.action,outcome.repaired),('repair',1))
        self.transport.assert_called_once()
        candidate.refresh_from_db();old.refresh_from_db();self.raw.refresh_from_db()
        self.assertEqual(candidate.pk,original_id)
        self.assertEqual(EventCandidate.objects.count(),1)
        self.assertEqual(candidate.title,'Гурт 0')
        self.assertEqual(candidate.start_time,datetime.fromisoformat(self.mapping['events'][0]['start_time']))
        self.assertEqual(candidate.category,'concert')
        self.assertEqual(candidate.venue_name,'Зал 0')
        self.assertTrue(candidate.source_fingerprint)
        self.assertEqual(old.candidate_id,original_id)
        self.assertEqual(old.match_status,'probable')
        self.assertEqual(old.facts,{'original':'keep'})
        self.assertIn('Original provenance',candidate.extraction_notes)
        self.assertIn('venue_name_provenance',candidate.extraction_notes)
        self.assertEqual(self.raw.raw_metadata['event_extraction_history'],[self.old_audit])
        self.assertTrue(self.raw.raw_metadata['event_extraction']['provider_retried'])

    def test_dry_run_previews_without_any_model_changes(self):
        candidate=self.draft()
        before=list(EventCandidate.objects.values())
        raw_before=deepcopy(self.raw.raw_metadata)
        output=self.command(reextract=True,dry_run=True)
        self.assertIn(f'Candidate #{candidate.pk} repair preview:',output)
        self.assertIn('title: [blank] -> Гурт 0',output)
        self.assertIn('start_time: [blank] -> ',output)
        self.assertIn('would become complete',output)
        self.assertIn('Candidates repaired (preview): 1',output)
        self.assertEqual(list(EventCandidate.objects.values()),before)
        self.assertFalse(EventSourceObservation.objects.exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata,raw_before)

    def test_real_command_reports_repairs_not_creations(self):
        candidate=self.draft()
        output=self.command(reextract=True)
        self.assertIn('Candidates repaired: 1',output)
        self.assertIn('Candidates created: 0',output)
        self.assertIn(f'Candidate #{candidate.pk} repaired:',output)
        self.assertEqual(EventCandidate.objects.count(),1)

    def test_complete_candidate_untouched_and_provider_not_called(self):
        candidate=self.draft(title='Complete',start_time=datetime.fromisoformat('2026-10-20T19:00:00+03:00'))
        before=list(EventCandidate.objects.values())
        raw_before=deepcopy(self.raw.raw_metadata)
        output=self.command(reextract=True)
        self.assertIn('Complete candidate preserved',output)
        self.transport.assert_not_called()
        self.assertEqual(list(EventCandidate.objects.values()),before)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata,raw_before)

    def test_nonblank_moderator_values_and_manual_unknown_price_are_preserved(self):
        candidate=self.draft(title='Moderator title',venue_name='Moderator venue',category='party',
            extraction_notes='[Candidate facts] Moderator edited: title,category\n[Location facts] Moderator edited: venue_name\n[Pricing] Moderator edited: price_status')
        self.raw.raw_text+='\n500 грн';self.raw.save()
        self.mapping['events'][0]['price']='500'
        self.mapping['events'][0]['evidence']['price']=[{'source':'caption','quote':'500 грн'}]
        self.transport.return_value=response(self.mapping)
        outcome=self.extract(reextract=True)
        self.assertEqual(outcome.repaired,1)
        candidate.refresh_from_db()
        self.assertEqual((candidate.title,candidate.venue_name,candidate.category),('Moderator title','Moderator venue','party'))
        self.assertEqual((candidate.price,candidate.price_status),(None,'unknown'))

    def test_moderator_owned_blank_start_from_admin_log_is_not_filled(self):
        candidate=self.draft(title='Moderator title')
        user=get_user_model().objects.create_user(username='repair-reviewer')
        LogEntry.objects.create(user=user,content_type=ContentType.objects.get_for_model(EventCandidate),
            object_id=str(candidate.pk),object_repr=candidate.title,action_flag=CHANGE,
            change_message=json.dumps([{'changed':{'fields':['start_time']}}]))
        before=list(EventCandidate.objects.values())
        outcome=self.extract(reextract=True)
        self.assertEqual(outcome.action,'repair_failed')
        self.assertEqual(outcome.repaired,0)
        self.assertIn('moderator-owned blank',outcome.unresolved[-1]['reasons']['start_time'][0])
        self.assertEqual(list(EventCandidate.objects.values()),before)

    def test_manual_start_retained_while_missing_title_is_filled(self):
        start=datetime.fromisoformat('2026-10-21T19:00:00+03:00')
        candidate=self.draft(start_time=start,extraction_notes='[Candidate facts] Moderator edited: start_time')
        self.extract(reextract=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.start_time,start)
        self.assertEqual(candidate.title,'Гурт 0')

    def test_provider_failure_keeps_candidate_and_records_new_reason(self):
        candidate=self.draft(title='Retain me')
        before=list(EventCandidate.objects.values())
        self.provider=Mock(extract=Mock(side_effect=ProviderUnavailable('provider_timeout')))
        output=self.command(reextract=True)
        self.assertIn('Provider retry failed: provider_timeout',output)
        self.assertIn('Candidate repairs needing review: 1',output)
        self.assertIn('Candidates created: 0',output)
        self.assertEqual(list(EventCandidate.objects.values()),before)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['event_extraction']['fallback_reason'],'provider_timeout')
        self.assertEqual(self.raw.raw_metadata['event_extraction_history'],[self.old_audit])
        self.assertEqual(self.raw.extraction_status,'extracted')

    def test_retry_bypasses_old_successful_openai_cache_too(self):
        self.draft()
        from .extraction import extract_facts
        extract_facts(self.raw,provider=self.provider)
        self.raw.save()
        self.raw.refresh_from_db()
        old=deepcopy(self.raw.raw_metadata['event_extraction'])
        self.transport.reset_mock()
        outcome=self.extract(reextract=True)
        self.transport.assert_called_once()
        self.assertEqual(outcome.repaired,1)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.raw_metadata['event_extraction_history'][-1],old)

    def test_multiple_upcoming_events_cannot_be_assigned_to_blank_draft(self):
        candidate=self.draft()
        self.set_events(2)
        before=list(EventCandidate.objects.values())
        outcome=self.extract(reextract=True)
        self.assertEqual(outcome.repaired,0)
        self.assertEqual(outcome.action,'repair_failed')
        self.assertEqual(list(EventCandidate.objects.values()),before)
        self.assertIn('No unique valid upcoming event',outcome.unresolved[-1]['reasons']['event'][0])

    def test_http_retry_failure_shows_candidate_and_sanitized_status(self):
        candidate=self.draft()
        rejected=response(self.mapping)
        rejected.status_code=429
        self.transport.return_value=rejected
        output=self.command(reextract=True,dry_run=True)
        self.assertIn('Provider: openai',output)
        self.assertIn('Provider retry failed: provider_http_failure (HTTP 429)',output)
        self.assertIn(f'Unresolved: Candidate #{candidate.pk}',output)
        self.assertNotIn('FAKE_TEST_KEY',output)
        candidate.refresh_from_db()
        self.assertEqual(candidate.title,'')
        self.assertIsNone(candidate.start_time)
