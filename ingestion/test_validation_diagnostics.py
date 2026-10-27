from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock
from io import StringIO
from django.test import TestCase,SimpleTestCase
from django.core.management import call_command
from . import test_multi_extraction as fixtures
from .extractors.contracts import ExtractionBatch,KYIV,ExtractionContext
from .extractors.parsers import parse_times
from .test_hybrid import response
from .deduplication import title_similarity


class DateEvidenceTests(SimpleTestCase):
    def test_publication_anchor_dates_and_conservative_rollover(self):
        reference=datetime(2026,10,6,12,tzinfo=KYIV)
        for text in ('9 жовтня 18:00','09.10 18:00','9.10 18:00','завтра 18:00','сьогодні 18:00','четвер 18:00'):
            start=parse_times(text,reference)[0]
            self.assertEqual(start.year,2026)
        self.assertEqual(parse_times('5 січня 18:00',datetime(2026,12,29,12,tzinfo=KYIV))[0].year,2027)
        self.assertEqual(parse_times('5 вересня 18:00',reference)[0].year,2026)

    def test_realistic_title_formats_and_reordering(self):
        for a,b in [('Sick Solution','Концерт Sick Solution у Львові'),('Наталка Полтавка','Вистава «Наталка Полтавка»'),
            ('Ланч-концерт. Орган та віолончель','Орган та віолончель — ланч-концерт')]:
            self.assertGreaterEqual(title_similarity(a,b),.85)


class StructuredValidationTests(TestCase):
    setUp=fixtures.MultiExtractionTests.setUp
    set_events=fixtures.MultiExtractionTests.set_events
    extract=fixtures.MultiExtractionTests.extract

    def payload(self):
        self.set_events(1)
        return deepcopy(self.mapping)

    def test_currency_price_does_not_discard_valid_event_and_empty_address(self):
        data=self.payload();data['events'][0]['price']='від 300 грн'
        batch=ExtractionBatch.from_mapping(data)
        self.assertFalse(batch.unresolved)
        self.assertEqual(batch.events[0].price,300)
        data['events'][0]['price']=None
        self.transport.return_value=response(data)
        outcome=self.extract()
        self.assertEqual(outcome.candidate.price_status,'unknown')
        self.assertIsNone(outcome.candidate.price)
        self.assertEqual(outcome.candidate.address,'')
        self.assertTrue(outcome.candidate.venue_name)

    def test_bad_start_has_field_reason(self):
        data=self.payload();data['events'][0]['start_time']='not-a-date'
        batch=ExtractionBatch.from_mapping(data)
        self.assertIn('start_time',batch.unresolved[0]['reasons'])

    def test_offset_correction_requires_matching_wall_time_evidence(self):
        data=self.payload();entry=data['events'][0]
        entry['start_time']='2026-10-25T12:00:00+03:00'
        entry['evidence']['start_time']=[{'source':'caption','quote':'25.10.2026 12:00'}]
        event=ExtractionBatch.from_mapping(data).events[0]
        self.assertEqual(event.start_time.hour,12)
        self.assertEqual(event.start_time.utcoffset().total_seconds(),7200)

    def test_past_start_and_command_report_explicit_field_reason(self):
        data=self.payload();entry=data['events'][0]
        entry['start_time']='2026-08-28T19:00:00+03:00'
        entry['evidence']['start_time']=[{'source':'caption','quote':'28.08.2026 19:00'}]
        self.raw.raw_text += '\n28.08.2026 19:00';self.raw.save()
        self.transport.return_value=response(data)
        output=StringIO()
        from unittest.mock import patch
        with patch('ingestion.management.commands.extract_events.configured_provider',return_value=self.provider):
            call_command('extract_events',source='festrepublic',dry_run=True,stdout=output)
        self.assertIn('start_time: event start is in the past',output.getvalue())

    def test_unsaved_collected_item_can_preview_without_persistence(self):
        from .models import RawSourceItem,EventCandidate
        data=self.payload()
        self.transport.return_value=response(data)
        count=RawSourceItem.objects.count()
        self.raw.pk=None
        outcome=self.extract(dry_run=True)
        self.assertEqual(outcome.ready,1)
        self.assertEqual(RawSourceItem.objects.count(),count)
        self.assertFalse(EventCandidate.objects.exists())

    def test_provider_omitting_past_event_keeps_local_reason(self):
        from .extractors.contracts import ExtractionBatch
        from .extraction import extract_raw_source_item
        self.raw.raw_text='Концерт: Орган\n28.08.2026 19:00'
        self.provider.extract=Mock(return_value=ExtractionBatch(False))
        outcome=extract_raw_source_item(self.raw,provider=self.provider,dry_run=True)
        self.assertEqual(outcome.ready,0)
        self.assertEqual(outcome.unresolved[0]['reasons']['start_time'],['event start is in the past'])

    def test_explicit_old_rescheduled_date_does_not_conflict_with_new_date(self):
        from .extractors.hybrid import evidenced_interval
        reference=datetime(2026,10,6,12,tzinfo=KYIV)
        start,_=evidenced_interval('нова дата: 10.10.2026 17:00 (було 25.09.2026 18:00)',reference)
        self.assertEqual(start,datetime(2026,10,10,17,tzinfo=KYIV))
        ambiguous,_=evidenced_interval('10.10.2026 17:00 та 25.09.2026 18:00',reference)
        self.assertIsNone(ambiguous)

    def test_single_event_location_date_price_in_separate_paragraphs_are_retained(self):
        data=self.payload()
        self.raw.raw_text=self.raw.raw_text.replace('\n','\n\n')+'\n\nВартість: 400 грн'
        self.raw.save()
        entry=data['events'][0];entry['price']='400'
        entry['evidence']['price']=[{'source':'caption','quote':'400 грн'}]
        self.transport.return_value=response(data)
        outcome=self.extract(dry_run=True)
        self.assertEqual(outcome.ready,1)
        self.assertTrue(outcome.results[0].venue_name)
        self.assertEqual(outcome.results[0].price,400)
