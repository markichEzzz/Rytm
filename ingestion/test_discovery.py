from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from django.core.management import call_command, CommandError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from . import tests as fixtures
from . import test_image_text as image_fixtures
from .collectors import CollectedSourceItem
from .deduplication import store_observation
from .extraction import extract_raw_source_item
from .image_text import prepare_image_text, download_image
from .models import SourceAccount, RawSourceItem, EventCandidate, EventSourceObservation
from .providers.telegram import parse_public_page, TelegramCollector, TelegramCollectionError
from .services import store_collected_item, approve_candidate

HTML = '''<div class="tgme_channel_info"></div><div class="tgme_widget_message" data-post="test_channel/42">
<div class="tgme_widget_message_text">Концерт: Jazz Band<br>11.11.2026 19:00<br>Місце: FESTrepublic<br>500 грн</div>
<a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/poster.jpg')"></a>
<time datetime="2026-10-09T09:00:00+00:00"></time></div>'''


class TelegramTests(TestCase):
    def setUp(self):
        self.source=SourceAccount.objects.create(platform='telegram',username='test_channel')
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Unit tests must not access live networks'))
        network.start()
        self.addCleanup(network.stop)

    def test_source_and_idempotent_seed_preserve_instagram_and_settings(self):
        SourceAccount.objects.create(username='festrepublic')
        call_command('seed_telegram_sources',stdout=StringIO())
        SourceAccount.objects.filter(username='kudypity_lviv').update(enabled=False)
        call_command('seed_telegram_sources',stdout=StringIO())
        self.assertEqual(SourceAccount.objects.filter(platform='telegram').count(),3)
        self.assertFalse(SourceAccount.objects.get(username='kudypity_lviv').enabled)
        self.source.full_clean()
        self.assertTrue(SourceAccount.objects.filter(platform='instagram').exists())

    def test_public_html_normalization_and_idempotence(self):
        item=parse_public_page(HTML,'test_channel',5)[0]
        self.assertEqual(item.external_id,'42')
        self.assertEqual(item.source_url,'https://t.me/test_channel/42')
        self.assertIn('\n11.11.2026',item.raw_text)
        self.assertIsNotNone(item.published_at.tzinfo)
        self.assertEqual(item.raw_image_url,'https://cdn4.telesco.pe/file/poster.jpg')
        self.assertTrue(store_collected_item(self.source,item)[1])
        self.assertFalse(store_collected_item(self.source,item)[1])
        self.assertEqual(RawSourceItem.objects.count(),1)

    def test_telegram_text_uses_shared_extraction_and_unknown_price(self):
        item=parse_public_page(HTML.replace('500 грн','Ціна невідома'),'test_channel',5)[0]
        raw,_=store_collected_item(self.source,item)
        raw.raw_image_url='';raw.raw_metadata['image_urls']=[];raw.save()
        outcome=extract_raw_source_item(raw)
        self.assertEqual(outcome.action,'created')
        self.assertEqual(outcome.candidate.category,'concert')
        self.assertEqual((outcome.candidate.price_status,outcome.candidate.price),('unknown',None))
        self.assertFalse(outcome.candidate.public_photo)

    def test_telegram_image_reuses_bounded_private_download_and_ocr(self):
        raw,_=store_collected_item(self.source,parse_public_page(HTML,'test_channel',5)[0])
        response=image_fixtures.DownloadTests.response(self)
        with patch('ingestion.image_text.requests.get',return_value=response):
            self.assertEqual(download_image(raw.raw_image_url),b'bytes')
        extractor=Mock(spec=['extract_text'],extract_text=Mock(return_value='Poster text'))
        prepare_image_text(raw,extractor=extractor,downloader=Mock(return_value=b'bytes'))
        self.assertIn('Poster text',raw.raw_metadata['image_text'])
        self.assertEqual(raw.raw_metadata['image_text_source'],'ocr')

    def test_access_blocked_and_empty_are_distinct(self):
        with self.assertRaises(TelegramCollectionError):
            parse_public_page('<html>Login required</html>','test_channel',5)
        self.assertEqual(parse_public_page('<div class="tgme_channel_info"></div>','test_channel',5),[])
        response=Mock(status_code=403);response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('ingestion.providers.telegram.requests.get',return_value=response):
            with self.assertRaises(TelegramCollectionError): TelegramCollector().fetch_items(self.source)

    def test_one_broken_source_does_not_abort_and_dry_run_has_no_writes(self):
        SourceAccount.objects.create(platform='telegram',username='second_channel')
        collector=Mock(fetch_items=Mock(side_effect=[TelegramCollectionError('access_blocked_or_unavailable'),parse_public_page(HTML,'test_channel',5)]))
        before=list(SourceAccount.objects.values())
        output=StringIO()
        with patch('ingestion.management.commands.collect_telegram.TelegramCollector',return_value=collector):
            with self.assertRaises(CommandError): call_command('collect_telegram',dry_run=True,stdout=output)
        self.assertEqual(collector.fetch_items.call_count,2)
        self.assertIn('Successful: 1',output.getvalue())
        self.assertFalse(RawSourceItem.objects.exists())
        self.assertEqual(list(SourceAccount.objects.values()),before)

    def test_command_collects_all_enabled_telegram_sources_idempotently(self):
        disabled=SourceAccount.objects.create(platform='telegram',username='disabled_channel',enabled=False)
        SourceAccount.objects.create(username='instagram_only')
        collector=Mock(fetch_items=Mock(return_value=parse_public_page(HTML,'test_channel',5)))
        with patch('ingestion.management.commands.collect_telegram.TelegramCollector',return_value=collector):
            call_command('collect_telegram',stdout=StringIO())
            call_command('collect_telegram',stdout=StringIO())
        self.assertEqual(RawSourceItem.objects.count(),1)
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.last_checked_at)
        self.assertEqual(collector.fetch_items.call_count,2)
        disabled.refresh_from_db()
        self.assertIsNone(disabled.last_checked_at)


class CrossSourceMatchingTests(TestCase):
    setUp=fixtures.IngestionTests.setUp
    candidate=fixtures.IngestionTests.candidate

    def observed(self,**kwargs):
        source=SourceAccount.objects.create(platform='telegram',username='telegram_'+str(SourceAccount.objects.count()))
        raw=RawSourceItem.objects.create(source_account=source,external_id='1',source_url='https://t.me/test/1')
        values=dict(title='Океан Ельзи у Львові',start_time=self.first.start_time,venue_name=self.first.venue_name,address=self.first.address,
            latitude=self.first.latitude,longitude=self.first.longitude,category='concert')
        values.update(kwargs)
        candidate=EventCandidate(raw_source_item=raw,**values)
        with transaction.atomic(): return store_observation(candidate)

    def initial(self,**kwargs):
        self.first=self.candidate(**{'title':'Концерт Океану Ельзи',**kwargs})
        return self.first

    def test_cross_platform_fuzzy_same_time_location_links_one_candidate(self):
        self.initial()
        canonical,added=self.observed()
        self.assertFalse(added)
        self.assertEqual(canonical.pk,self.first.pk)
        self.assertEqual(EventCandidate.objects.count(),1)
        self.assertEqual(self.first.observations.get().match_status,'confirmed')
        canonical,added=self.observed()
        self.assertFalse(added)
        self.assertEqual(self.first.observations.count(),2)

    def test_same_venue_and_date_different_title_is_distinct(self):
        self.initial()
        _,added=self.observed(title='Jazz band')
        self.assertTrue(added)

    def test_same_title_on_different_day_is_distinct(self):
        self.initial()
        _,added=self.observed(title=self.first.title,start_time=self.first.start_time+timedelta(days=1))
        self.assertTrue(added)

    def test_time_disagreement_stays_in_review_with_link(self):
        self.initial()
        candidate,added=self.observed(start_time=self.first.start_time+timedelta(minutes=30))
        self.assertTrue(added)
        self.assertEqual(candidate.status,'new')
        observation=candidate.observations.get()
        self.assertEqual(observation.match_status,'probable')
        self.assertEqual(observation.matched_candidate,self.first)

    def test_manual_values_are_never_overwritten(self):
        self.initial(price=0,price_status='free',extraction_notes='[Pricing] Moderator edited: price_status')
        self.observed(price=700,price_status='paid')
        self.first.refresh_from_db()
        self.assertEqual((self.first.price_status,self.first.price),('free',0))

    def test_stronger_price_fills_untouched_unknown_but_manual_unknown_wins(self):
        self.initial(price=None,price_status='unknown')
        self.observed(price=700,price_status='paid')
        self.first.refresh_from_db()
        self.assertEqual(self.first.price,700)
        self.first.price=None
        self.first.price_status='unknown'
        self.first.extraction_notes += '\n[Pricing] Moderator edited: price_status'
        self.first.save()
        self.observed(price=900,price_status='paid')
        self.first.refresh_from_db()
        self.assertEqual((self.first.price_status,self.first.price),('unknown',None))

    def test_approved_candidate_is_not_recreated_and_observation_links_public_event(self):
        self.initial()
        event,_=approve_candidate(self.first,self.reviewer)
        candidate,added=self.observed()
        self.assertFalse(added)
        self.assertEqual(candidate.pk,self.first.pk)
        self.assertEqual(candidate.observations.get().event,event)
        self.assertEqual(EventCandidate.objects.count(),1)

    def test_repeated_observation_is_idempotent(self):
        self.initial()
        self.observed()
        observation=EventSourceObservation.objects.get()
        candidate=EventCandidate(raw_source_item=observation.raw_source_item,title='Океан Ельзи у Львові',start_time=self.first.start_time,
            venue_name=self.first.venue_name,address=self.first.address)
        with transaction.atomic(): store_observation(candidate)
        self.assertEqual(EventSourceObservation.objects.count(),1)

    def test_stable_fingerprint_links_without_overwriting_corrected_title(self):
        from .extraction import event_identity
        from .extractors.contracts import ExtractionResult
        self.initial(title='Original artist')
        self.first.source_fingerprint=event_identity(ExtractionResult(True,title=self.first.title,
            start_time=self.first.start_time,venue_name=self.first.venue_name))[0]
        self.first.title='Moderator corrected artist'
        self.first.save()
        canonical,added=self.observed(title='Original artist')
        self.assertFalse(added)
        self.assertEqual(canonical.title,'Moderator corrected artist')

    def test_generic_concert_title_is_not_a_reliable_fingerprint(self):
        from .extraction import event_identity
        from .extractors.contracts import ExtractionResult
        self.initial(title='Концерт')
        self.first.source_fingerprint=event_identity(ExtractionResult(True,title=self.first.title,
            start_time=self.first.start_time,venue_name=self.first.venue_name))[0]
        self.first.save()
        _,added=self.observed(title='Концерт')
        self.assertTrue(added)


class RealisticTitleMatchingTests(CrossSourceMatchingTests):
    def test_requested_title_variants_link_pending_and_approved(self):
        from events.models import Event
        pairs=[('Sick Solution','Концерт Sick Solution у Львові'),
            ('Наталка Полтавка','Вистава «Наталка Полтавка»'),
            ('Ланч-концерт. Орган та віолончель','Орган та віолончель — ланч-концерт')]
        for index,(original,formatted) in enumerate(pairs):
            self.initial(title=original,start_time=timezone.now()+timedelta(days=10+index))
            canonical,added=self.observed(title=formatted)
            self.assertFalse(added)
            self.assertEqual(canonical.pk,self.first.pk)
            event,_=approve_candidate(self.first,self.reviewer)
            canonical,added=self.observed(title=formatted)
            self.assertFalse(added)
            self.assertEqual(canonical.pk,self.first.pk)
            self.assertEqual(canonical.observations.filter(event=event).count(),2)
        self.assertEqual(Event.objects.count(),3)
        self.assertEqual(EventCandidate.objects.count(),3)
