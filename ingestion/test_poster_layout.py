"""Deterministic poster fixtures: spatial OCR and mocked semantic output, no live services."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.contrib import admin
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from .admin import EventCandidateAdmin
from .extraction import extract_raw_source_item
from .extractors.contracts import ExtractionBatch, ExtractionResult, EVIDENCE_FIELDS, KYIV
from .extractors.openai import OpenAIEventExtractionProvider, ProviderUnavailable
from .image_text import TesseractImageTextExtractor, prepare_image_text
from .models import EventCandidate, RawSourceItem, SourceAccount
from .poster_layout import poster_context, MAX_BLOCKS
from .test_hybrid import response

NOW = datetime(2026, 10, 21, 12, tzinfo=KYIV)


def block(text, y, x=20, confidence=94, width=340):
    return dict(text=text, x=x, y=y, width=width, height=20, confidence=confidence)


def rows(title='Гурт А', day=25, hour=19, y=100, x=20, price='500 грн', venue='Зал А'):
    return [block('Концерт: ' + title, y, x), block(f'{day} жовтня {hour}:00', y+30, x),
            block('Місце: ' + venue, y+60, x), block(price, y+90, x)]


def result(title='Гурт А', day=25, hour=19, price='500', venue='Зал А', source='ocr', quotes=None):
    evidence = {key: [] for key in EVIDENCE_FIELDS}
    values = dict(title=title, start_time=f'{day} жовтня {hour}:00', venue_name='Місце: ' + venue,
                  category='Концерт: ' + title, price=('Від ' if price == '300' else '') + price + ' грн' if price else '')
    values.update(quotes or {})
    for key, quote in values.items():
        if quote:
            evidence[key] = [{'source': source, 'quote': quote}]
    return ExtractionResult(True, title=title, start_time=datetime(2026, 10, day, hour, tzinfo=KYIV),
        venue_name=venue, price=Decimal(price) if price else None, category='concert', event_count=1,
        confidence=.95, needs_review=False, evidence=evidence)


def raw_layout(blocks, caption=''):
    return SimpleNamespace(raw_text=caption, raw_metadata={'image_text': '\n'.join(b['text'] for b in blocks),
        'image_text_layout': [{'image_index': 1, 'width': 1000, 'height': 1200, 'blocks': blocks}]})


class LayoutUnitTests(SimpleTestCase):
    def test_tesseract_tsv_preserves_lines_coordinates_confidence_and_plain_text(self):
        data=BytesIO();Image.new('RGB',(500,500)).save(data,'PNG')
        tsv='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv+='5\t1\t1\t1\t1\t1\t20\t40\t80\t20\t90\tКонцерт\n'
        tsv+='5\t1\t1\t1\t1\t2\t110\t40\t70\t20\t80\tГурт\n'
        with patch('ingestion.image_text.subprocess.run',return_value=Mock(stdout=tsv.encode())) as run:
            output=TesseractImageTextExtractor('/local/tesseract').extract_layout(data.getvalue())
        self.assertEqual(output['text'],'Концерт Гурт')
        self.assertEqual(output['blocks'][0]['x'],20);self.assertEqual(output['blocks'][0]['width'],160)
        self.assertEqual(output['blocks'][0]['confidence'],85)
        self.assertEqual(run.call_args.args[0][-1],'tessedit_create_tsv=1');self.assertEqual(run.call_count,1)

    def test_unexpected_plain_stdout_is_not_cached_as_successful_empty_tsv(self):
        from .image_text import ImageTextError
        data=BytesIO();Image.new('RGB',(20,20)).save(data,'PNG')
        with patch('ingestion.image_text.subprocess.run',return_value=Mock(stdout=b'Unstructured poster text')):
            with self.assertRaisesMessage(ImageTextError,'local_ocr_invalid_layout'):
                TesseractImageTextExtractor('/local/tesseract').extract_layout(data.getvalue())

    def test_two_vertical_events_and_two_columns(self):
        for blocks in [rows()+rows('Гурт Б',26,20,400),rows()+rows('Гурт Б',26,20,100,600)]:
            layout=poster_context(raw_layout(blocks))
            self.assertTrue(layout['detected_multi_event_poster']);self.assertEqual(len(layout['groups']),2)
            self.assertIn('Гурт А',layout['groups'][0]['text']);self.assertNotIn('Гурт Б',layout['groups'][0]['text'])

    def test_day_heading_with_multiple_inline_events(self):
        blocks=[block('25 жовтня',20),block('19:00 Концерт: Гурт А',60),block('21:00 Концерт: Гурт Б',90),
                block('26 жовтня',130),block('18:00 Концерт: Гурт В',170)]
        layout=poster_context(raw_layout(blocks))
        self.assertEqual(len(layout['groups']),3)
        self.assertEqual([g['date_context'] for g in layout['groups']],['25 жовтня','25 жовтня','26 жовтня'])

    def test_one_multi_day_festival_is_not_automatically_split(self):
        layout=poster_context(raw_layout([block('Фестиваль музики',50),block('25–27 жовтня',80),block('19:00 – 22:00',110)]))
        self.assertFalse(layout['detected_multi_event_poster'])

    def test_repeated_carousel_image_is_not_proof_of_two_events(self):
        raw=raw_layout(rows());raw.raw_metadata['image_text_layout']*=2
        self.assertFalse(poster_context(raw)['detected_multi_event_poster'])

    def test_metadata_allowlist_and_bounded_blocks(self):
        raw=raw_layout([block('Концерт',i*25) for i in range(MAX_BLOCKS+50)])
        raw.raw_metadata['image_text_layout'][0]['cookies']='PRIVATE_COOKIE'
        raw.raw_metadata['image_text_layout'][0]['blocks'][0]['token']='PRIVATE_TOKEN'
        raw.raw_metadata['browser_storage']='PRIVATE_BROWSER'
        layout=poster_context(raw)
        self.assertLessEqual(layout['ocr_block_count'],MAX_BLOCKS)
        self.assertNotIn('PRIVATE',str(layout));self.assertTrue(layout['warnings'])

    def test_overlapping_blocks_are_ambiguous(self):
        layout=poster_context(raw_layout(rows()+[block('Інша назва',105)]))
        self.assertTrue(layout['groups'][0]['warnings'])


class PosterExtractionTests(TestCase):
    def setUp(self):
        self.source=SourceAccount.objects.create(username='poster_account')
        self.raw=RawSourceItem.objects.create(source_account=self.source,external_id='poster',published_at=NOW,
            source_url='https://www.instagram.com/p/poster/')
        clock=patch('django.utils.timezone.now',return_value=NOW);clock.start();self.addCleanup(clock.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No live services in fixtures'));network.start();self.addCleanup(network.stop)

    def setup_poster(self, blocks, events, caption='', shared=None):
        self.raw.raw_metadata=raw_layout(blocks).raw_metadata;self.raw.raw_text=caption;self.raw.save()
        self.provider=Mock(extract=Mock(return_value=ExtractionBatch(True,tuple(events),shared_context=shared or {})))
        return self.raw

    def extract(self, **kwargs):
        return extract_raw_source_item(self.raw,provider=self.provider,**kwargs)

    def test_single_event_still_one_candidate(self):
        self.setup_poster(rows(),[result()]);outcome=self.extract()
        self.assertEqual(len(outcome.candidates),1);self.assertEqual(outcome.candidate.price,Decimal('500'))
        self.assertIn('[Poster] group=1',outcome.candidate.extraction_notes)

    def test_two_separate_events_provenance_and_no_sibling_merge(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400,venue='Зал Б'),[result(),result('Гурт Б',26,20,venue='Зал Б')])
        outcome=self.extract();self.assertEqual(len(outcome.candidates),2)
        self.assertEqual({c.raw_source_item_id for c in outcome.candidates},{self.raw.pk})
        self.raw.refresh_from_db();audit=self.raw.raw_metadata['event_extraction']
        self.assertEqual([e['event_group_index'] for e in audit['poster_events']],[1,2])
        self.assertEqual(RawSourceItem.objects.count(),1)

    def test_shared_venue_three_events_only_explicit_header(self):
        header='Усі події у FESTrepublic'
        blocks=[block(header,10,width=900)]
        events=[]
        for i,title in enumerate(['Гурт А','Гурт Б','Гурт В']):
            blocks+=rows(title,25+i,19,100+i*280,venue='FESTrepublic')[:2]
            item=result(title,25+i,venue='FESTrepublic',price=None)
            events.append(replace(item,venue_name=None,evidence={k:v for k,v in item.evidence.items() if k!='venue_name'}))
        self.setup_poster(blocks,events,shared={'venue_name':'FESTrepublic','address':None,
            'evidence':{'venue_name':[{'source':'ocr','quote':header}],'address':[]}})
        outcome=self.extract();self.assertEqual(len(outcome.candidates),3)
        self.assertEqual({c.venue_name for c in outcome.candidates},{'FESTrepublic'})
        self.assertTrue(all(c.price is None and c.price_status=='unknown' for c in outcome.candidates))

    def test_bare_footer_venue_is_not_inherited(self):
        events=[replace(result(),venue_name=None,evidence={k:v for k,v in result().evidence.items() if k!='venue_name'}),replace(result('Гурт Б',26),venue_name=None,evidence={k:v for k,v in result('Гурт Б',26).evidence.items() if k!='venue_name'})]
        self.setup_poster(rows()[:2]+rows('Гурт Б',26,y=400)[:2]+[block('FESTrepublic',700)],events)
        outcome=self.extract();self.assertEqual(len(outcome.candidates),2);self.assertTrue(all(not c.venue_name for c in outcome.candidates))

    def test_cross_group_date_cannot_create_confident_wrong_event(self):
        wrong=replace(result(),start_time=datetime(2026,10,26,20,tzinfo=KYIV),evidence={**result().evidence,'start_time':[{'source':'ocr','quote':'26 жовтня 20:00'}]})
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[wrong,result('Гурт Б',26,20)])
        outcome=self.extract();self.assertEqual(len(outcome.candidates),1)
        self.assertEqual(outcome.candidate.title,'Гурт Б');self.assertTrue(outcome.unresolved)
        self.assertIn('start_time',outcome.unresolved[0]['reasons'])

    def test_prices_do_not_cross_and_from_price_preserved(self):
        a=replace(result(),price=Decimal('700'),evidence={**result().evidence,'price':[{'source':'ocr','quote':'700 грн'}]})
        self.setup_poster(rows()+rows('Гурт Б',26,20,400,price='700 грн'),[a,result('Гурт Б',26,20,price='700')])
        outcome=self.extract();self.assertEqual(len(outcome.candidates),2)
        self.assertIsNone(outcome.candidates[0].price);self.assertEqual(outcome.candidates[0].price_status,'unknown')
        self.assertEqual(outcome.candidates[1].price,Decimal('700'))
        self.raw.external_id='from-price';self.raw.pk=None;self.raw.extraction_status='new';self.raw.save()
        self.setup_poster(rows('Інший гурт',27,price='Від 300 грн'),[result('Інший гурт',27,price='300')])
        self.assertEqual(self.extract().candidate.price,Decimal('300'))

    def test_low_confidence_critical_date_requires_review(self):
        blocks=rows();blocks[1]['confidence']=15
        self.setup_poster(blocks,[result()]);outcome=self.extract()
        self.assertFalse(outcome.candidates);self.assertTrue(outcome.unresolved)
        self.assertIn('low-confidence',' '.join(outcome.results[0].ambiguities))

    def test_ambiguous_titles_around_one_time_are_not_guessed(self):
        blocks=[block('Концерт: Гурт А',50),block('25 жовтня 19:00',90),block('Концерт: Гурт Б',130)]
        self.setup_poster(blocks,[result(price=None),result('Гурт Б',price=None)])
        outcome=self.extract();self.assertFalse(outcome.candidates);self.assertEqual(len(outcome.unresolved),2)
        self.assertIn('unassigned time',' '.join(outcome.results[0].ambiguities))

    def test_caption_multi_events_survive_weak_image_ocr(self):
        a=result(source='caption');b=result('Гурт Б',26,20,source='caption')
        caption='Концерт: Гурт А\n25 жовтня 19:00\nМісце: Зал А\n500 грн\n\nКонцерт: Гурт Б\n26 жовтня 20:00\nМісце: Зал А\n500 грн'
        self.setup_poster([block('???',30,confidence=5)],[a,b],caption)
        self.assertEqual(len(self.extract().candidates),2)

    def test_past_and_future_keep_existing_policy(self):
        self.setup_poster(rows(day=20)+rows('Гурт Б',26,20,400),[result(day=20),result('Гурт Б',26,20)])
        outcome=self.extract();self.assertEqual(len(outcome.candidates),1)
        self.assertIn('past',outcome.unresolved[0]['reasons']['start_time'][0])

    def test_same_date_different_inline_times_use_shared_day_heading(self):
        blocks=[block('25 жовтня',20),block('19:00 Концерт: Гурт А',60),block('21:00 Концерт: Гурт Б',90)]
        events=[]
        for title,hour in [('Гурт А',19),('Гурт Б',21)]:
            item=result(title,hour=hour,price=None)
            evidence={**item.evidence,'start_time':[{'source':'ocr','quote':'25 жовтня'},{'source':'ocr','quote':f'{hour}:00 Концерт: {title}'}],
                'category':[{'source':'ocr','quote':f'{hour}:00 Концерт: {title}'}]}
            events.append(replace(item,venue_name=None,evidence={k:v for k,v in evidence.items() if k!='venue_name'}))
        self.setup_poster(blocks,events);outcome=self.extract()
        self.assertEqual([c.start_time.hour for c in outcome.candidates],[19,21])

    def test_provider_failure_and_deterministic_only_defer_multi_poster(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[])
        self.provider.extract.side_effect=ProviderUnavailable('provider_timeout')
        outcome=self.extract(dry_run=True);self.assertEqual(outcome.action,'review');self.assertFalse(EventCandidate.objects.exists())
        self.assertIn('without reliable',outcome.unresolved[0]['reason'])
        outcome=extract_raw_source_item(self.raw,dry_run=True)
        self.assertEqual(outcome.action,'review');self.assertFalse(outcome.candidates)

    def test_dry_run_is_completely_write_free(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        before=RawSourceItem.objects.get(pk=self.raw.pk).raw_metadata
        outcome=self.extract(dry_run=True);self.assertEqual(outcome.ready,2)
        self.raw.refresh_from_db();self.assertEqual(before,self.raw.raw_metadata);self.assertFalse(EventCandidate.objects.exists())

    def test_similar_names_distinct_times_are_not_deduplicated(self):
        self.setup_poster(rows('Гурт Альфа',25,19)+rows('Гурт Альфа live',25,21,400),[result('Гурт Альфа',25,19),result('Гурт Альфа live',25,21)])
        self.assertEqual(len(self.extract().candidates),2)

    def test_cross_source_duplicate_still_links(self):
        self.setup_poster(rows(),[result()]);first=self.extract().candidate
        other=SourceAccount.objects.create(username='other_poster')
        self.raw.pk=None;self.raw.source_account=other;self.raw.extraction_status='new';self.raw.save()
        self.setup_poster(rows(),[result()]);outcome=self.extract()
        self.assertEqual(outcome.linked,1);self.assertEqual(EventCandidate.objects.count(),1)
        self.assertEqual(first.observations.count(),2)

    def test_complete_legacy_identity_preserved_and_split_flagged(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        candidate=EventCandidate.objects.create(raw_source_item=self.raw,title='Moderator title',start_time=datetime(2026,10,25,19,tzinfo=KYIV),
            extraction_notes='[Candidate facts] Moderator edited: title',price_status='unknown')
        before=candidate.__dict__.copy();outcome=self.extract(reextract=True)
        candidate.refresh_from_db();self.assertEqual(candidate.title,before['title']);self.assertEqual(outcome.action,'skipped')
        self.provider.extract.assert_not_called();self.raw.refresh_from_db();self.assertIn('poster_split_review',self.raw.raw_metadata)

    def test_admin_shows_private_groups_and_sibling_links(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        outcome=self.extract();candidate=outcome.candidates[0]
        model_admin=EventCandidateAdmin(EventCandidate,admin.site)
        self.assertIn('event_group_index',str(model_admin.poster_diagnostics(candidate)))
        self.assertIn('Гурт Б',str(model_admin.sibling_candidates(candidate)))
        self.assertNotIn('poster_layout',self.client.get('/api/events/').content.decode())

    def test_strict_provider_input_contains_layout_but_not_browser_metadata(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        self.raw.raw_metadata['cookies']='PRIVATE_SECRET';self.raw.raw_metadata['storage_state']={'token':'PRIVATE_TOKEN'}
        from .extractors.hybrid import batch_mapping
        transport=Mock(return_value=response(batch_mapping(self.provider.extract.return_value)))
        provider=OpenAIEventExtractionProvider(api_key='test',model='fixture',transport=transport)
        extract_raw_source_item(self.raw,provider=provider,dry_run=True)
        payload=transport.call_args.kwargs['json'];self.assertIn('poster_layout',payload['input'][0]['content'][0]['text'])
        self.assertNotIn('PRIVATE_',str(payload));self.assertEqual(transport.call_count,1)
        self.assertTrue(payload['text']['format']['strict'])

    def test_multiple_days_several_events_each_day(self):
        blocks=[];events=[]
        for index,(day,hour,title) in enumerate([(25,19,'Гурт А'),(25,21,'Гурт Б'),(26,18,'Гурт В'),(26,20,'Гурт Г')]):
            if index in (0,2):blocks.append(block(f'{day} жовтня',20+index*70))
            quote=f'{hour}:00 Концерт: {title}'
            blocks.append(block(quote,50+index*70))
            item=result(title,day,hour,price=None)
            events.append(replace(item,venue_name=None,evidence={'title':[{'source':'ocr','quote':title}],
                'start_time':[{'source':'ocr','quote':f'{day} жовтня'},{'source':'ocr','quote':quote}],
                'category':[{'source':'ocr','quote':quote}]}))
        self.setup_poster(blocks,events);outcome=self.extract()
        self.assertEqual([(c.start_time.day,c.start_time.hour) for c in outcome.candidates],[(25,19),(25,21),(26,18),(26,20)])

    def test_range_heading_does_not_invent_day_for_independent_rows(self):
        blocks=[block('25–27 жовтня',20),block('19:00 Концерт: Гурт А',60),block('21:00 Концерт: Гурт Б',90)]
        events=[]
        for title,hour in [('Гурт А',19),('Гурт Б',21)]:
            item=result(title,hour=hour,price=None)
            events.append(replace(item,venue_name=None,evidence={'title':[{'source':'ocr','quote':title}],
                'start_time':[{'source':'ocr','quote':'25–27 жовтня'},{'source':'ocr','quote':f'{hour}:00 Концерт: {title}'}]}))
        self.setup_poster(blocks,events);outcome=self.extract();self.assertFalse(outcome.candidates)
        self.assertEqual(len(outcome.unresolved),2)

    def test_weekday_heading_uses_source_date_and_kyiv_timezone(self):
        blocks=[block("П'ятниця",20),block('19:00 Концерт: Гурт А',60),block('21:00 Концерт: Гурт Б',90)]
        events=[]
        for title,hour in [('Гурт А',19),('Гурт Б',21)]:
            item=result(title,23,hour,price=None)
            events.append(replace(item,venue_name=None,evidence={'title':[{'source':'ocr','quote':title}],
                'start_time':[{'source':'ocr','quote':"П'ятниця"},{'source':'ocr','quote':f'{hour}:00 Концерт: {title}'}]}))
        self.setup_poster(blocks,events);outcome=self.extract()
        self.assertEqual([c.start_time.day for c in outcome.candidates],[23,23])

    def test_explicit_free_stays_zero_and_missing_price_is_unknown(self):
        a=replace(result(price='0'),evidence={**result().evidence,'price':[{'source':'ocr','quote':'Вхід вільний'}]})
        b=result('Гурт Б',26,20,price=None)
        self.setup_poster(rows(price='Вхід вільний')+rows('Гурт Б',26,20,400,price=''),[a,b])
        outcome=self.extract();self.assertEqual([(c.price_status,c.price) for c in outcome.candidates],[('free',Decimal('0')),('unknown',None)])

    def test_incomplete_legacy_split_is_not_destructively_assigned(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        candidate=EventCandidate.objects.create(raw_source_item=self.raw,extraction_notes='[Candidate facts] Moderator edited: title')
        outcome=self.extract(reextract=True);candidate.refresh_from_db()
        self.assertEqual(candidate.title,'');self.assertIsNone(candidate.start_time)
        self.assertEqual(EventCandidate.objects.count(),1);self.assertTrue(any('Additional split event' in e['reason'] for e in outcome.unresolved))

    def test_plain_only_engine_and_layout_engine_both_keep_plain_ocr(self):
        raw=SimpleNamespace(raw_image_url='https://cdn.test.cdninstagram.com/a.jpg',raw_metadata={})
        extractor=Mock(spec=['extract_layout']);extractor.extract_layout.return_value={'text':'Poster text','blocks':[block('Poster text',20)],'width':500,'height':700}
        prepare_image_text(raw,extractor=extractor,downloader=lambda url:b'image')
        self.assertIn('Poster text',raw.raw_metadata['image_text']);self.assertEqual(len(raw.raw_metadata['image_text_layout']),1)
        prepare_image_text(raw,extractor=extractor,downloader=lambda url:b'image');self.assertEqual(extractor.extract_layout.call_count,1)

    def test_dry_run_command_reports_group_diagnostics(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result(),result('Гурт Б',26,20)])
        output=StringIO()
        with patch('ingestion.management.commands.extract_events.configured_provider',return_value=self.provider):
            call_command('extract_events',source='poster_account',platform='instagram',dry_run=True,stdout=output)
        self.assertIn('OCR blocks: 8',output.getvalue());self.assertIn('Logical groups: 2',output.getvalue())
        self.assertFalse(EventCandidate.objects.exists())

    def test_caption_day_heading_and_rows_without_spatial_ocr(self):
        caption="П'ятниця\n\n19:00 Концерт: Гурт А\n21:00 Концерт: Гурт Б"
        events=[]
        for title,hour in [('Гурт А',19),('Гурт Б',21)]:
            events.append(replace(result(title,23,hour,price=None,source='caption'),venue_name=None,
                evidence={'title':[{'source':'caption','quote':title}],
                    'start_time':[{'source':'caption','quote':"П'ятниця"},{'source':'caption','quote':f'{hour}:00 Концерт: {title}'}]}))
        self.setup_poster([],events,caption);outcome=self.extract()
        self.assertEqual([c.start_time.hour for c in outcome.candidates],[19,21])
        self.assertEqual({c.start_time.day for c in outcome.candidates},{23})

    def test_missing_model_entry_is_surfaced_as_unresolved_group(self):
        self.setup_poster(rows()+rows('Гурт Б',26,20,400),[result()]);outcome=self.extract()
        self.assertEqual(len(outcome.candidates),1);self.assertTrue(any('not reliably extracted' in e['reason'] for e in outcome.unresolved))
        self.raw.refresh_from_db();self.assertEqual(self.raw.extraction_status,'new')

    def test_low_confidence_shared_day_heading_does_not_assign_dates(self):
        blocks=[block('25 жовтня',20,confidence=5),block('19:00 Концерт: Гурт А',60),block('21:00 Концерт: Гурт Б',90)]
        events=[]
        for title,hour in [('Гурт А',19),('Гурт Б',21)]:
            events.append(replace(result(title,hour=hour,price=None),venue_name=None,
                evidence={'title':[{'source':'ocr','quote':title}],
                    'start_time':[{'source':'ocr','quote':'25 жовтня'},{'source':'ocr','quote':f'{hour}:00 Концерт: {title}'}]}))
        self.setup_poster(blocks,events);self.assertFalse(self.extract().candidates)

    def test_similar_names_same_slot_remain_distinct_sibling_candidates(self):
        self.setup_poster(rows('Гурт Альфа')+rows('Гурт Альфа live',25,19,400),[result('Гурт Альфа'),result('Гурт Альфа live')])
        outcome=self.extract();self.assertEqual(len(outcome.candidates),2);self.assertEqual(outcome.duplicates,0)

    def test_english_month_in_poster_keeps_explicit_date(self):
        blocks=rows();blocks[1]['text']='25 OCT 19:00'
        item=replace(result(),evidence={**result().evidence,'start_time':[{'source':'ocr','quote':'25 OCT 19:00'}]})
        self.setup_poster(blocks,[item]);candidate=self.extract().candidate
        self.assertIsNotNone(candidate);self.assertEqual(candidate.start_time.day,25)

    def test_low_confidence_date_word_not_hidden_by_high_line_average(self):
        blocks=rows();blocks[1]['confidence']=90;blocks[1]['confidence_min']=5
        self.setup_poster(blocks,[result()]);self.assertFalse(self.extract().candidates)
