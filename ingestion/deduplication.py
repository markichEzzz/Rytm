"""Conservative platform-independent matching; uncertain observations stay in review."""
from datetime import timedelta
from difflib import SequenceMatcher
import re
from zoneinfo import ZoneInfo

from django.db import connection
from events.models import Event
from .models import EventCandidate, EventSourceObservation
from .services import normalize_fact


def title_key(text):
    text = normalize_fact(text)
    return re.sub(r'\b(?:концерт|concert|вистава|у львові|у львовi|live)\b', '', text).strip()


def title_similarity(first,second):
    a,b=title_key(first),title_key(second)
    if not a or not b: return 0.0
    return max(SequenceMatcher(None,a,b).ratio(),SequenceMatcher(None,' '.join(sorted(a.split())),' '.join(sorted(b.split()))).ratio())


def match_level(candidate, other):
    if not candidate.start_time or not other.start_time:
        return None
    a,b=candidate.start_time,other.start_time
    if a.astimezone(ZoneInfo('Europe/Kyiv')).date() != b.astimezone(ZoneInfo('Europe/Kyiv')).date():
        return None
    delta=abs((a-b).total_seconds())
    if delta > 1800:
        return None
    first,second=title_key(candidate.title),title_key(other.title)
    if not first or not second or title_similarity(candidate.title,other.title) < .85:
        return None
    venue = getattr(other,'venue_name','')
    if isinstance(other,Event):
        match=re.search(r'Місце:\s*(.*?)\.\s*(?:Адреса:|Початок:|$)',other.description)
        venue=match[1] if match else ''
    same_venue=bool(candidate.venue_name and venue and normalize_fact(candidate.venue_name)==normalize_fact(venue))
    same_address=bool(candidate.address and other.address and normalize_fact(candidate.address)==normalize_fact(other.address))
    other_lat=getattr(other,'latitude',None)
    other_lon=getattr(other,'longitude',None)
    if isinstance(other,Event) and other.location:
        other_lat,other_lon=other.location.y,other.location.x
    nearby=all(v is not None for v in (candidate.latitude,candidate.longitude,other_lat,other_lon)) and (
        ((candidate.latitude-other_lat)*111000)**2+((candidate.longitude-other_lon)*72000)**2 <= 75**2)
    if not (same_venue or same_address or nearby):
        return None
    return 'confirmed' if delta == 0 else 'probable'


def find_match(candidate, fingerprint=None):
    if not candidate.start_time or not candidate.title:
        return None,None
    if fingerprint and title_key(candidate.title) and (candidate.venue_name or candidate.address):
        from django.db.models import Q
        exact=list(EventCandidate.objects.exclude(raw_source_item_id=candidate.raw_source_item_id).exclude(status__in=['rejected','duplicate']).filter(
            Q(source_fingerprint=fingerprint)|Q(observations__fingerprint=fingerprint)).distinct().order_by('pk')[:2])
        if exact:
            return exact[0], 'confirmed' if len(exact)==1 else 'probable'
    pool=EventCandidate.objects.exclude(raw_source_item_id=candidate.raw_source_item_id).exclude(status__in=['rejected','duplicate']).filter(
        start_time__gte=candidate.start_time-timedelta(minutes=30),start_time__lte=candidate.start_time+timedelta(minutes=30)).order_by('pk')
    matches=[]
    for other in pool:
        level=match_level(candidate,other)
        if level:
            matches.append((other,level))
    events=Event.objects.exclude(status='rejected').filter(start_time__gte=candidate.start_time-timedelta(minutes=30),start_time__lte=candidate.start_time+timedelta(minutes=30)).exclude(ingestion_candidate__isnull=False)
    for other in events:
        level=match_level(candidate,other)
        if level:
            matches.append((other,level))
    if len(matches)==1:
        return matches[0]
    return (matches[0][0],'probable') if matches else (None,None)


def store_observation(candidate):
    """Caller holds transaction. Serialize matching/creation with the approval lock."""
    from .extraction import event_identity
    from .extractors.contracts import ExtractionResult
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)',[742_691_001])
    from hashlib import sha256
    import json
    fingerprint=candidate.source_fingerprint or (event_identity(ExtractionResult(True,title=candidate.title,start_time=candidate.start_time,venue_name=candidate.venue_name,address=candidate.address))[0] if candidate.start_time else sha256(json.dumps([candidate.title,candidate.venue_name,candidate.address]).encode()).hexdigest())
    existing=EventSourceObservation.objects.filter(raw_source_item=candidate.raw_source_item,fingerprint=fingerprint).first()
    if existing and (existing.candidate_id or existing.event_id):
        return existing.candidate,False
    if existing:
        existing.delete()
    other,level=find_match(candidate,fingerprint)
    facts={name:getattr(candidate,name) for name in ('title','venue_name','address','category','price_status')}
    facts.update(start_time=candidate.start_time.isoformat() if candidate.start_time else None,price=str(candidate.price) if candidate.price is not None else None)
    if level=='confirmed':
        canonical=EventCandidate.objects.select_for_update().get(pk=other.pk) if isinstance(other,EventCandidate) else None
        public=canonical.event if canonical else other
        EventSourceObservation.objects.create(raw_source_item=candidate.raw_source_item,fingerprint=fingerprint,
            candidate=canonical,event=public,match_status='confirmed',facts=facts)
        # Fill only evidence-backed blanks; either manually edited pricing field protects both.
        if canonical and canonical.status=='new':
            from .extraction import enrich_missing_location
            suggestion=ExtractionResult(True,venue_name=candidate.venue_name,address=candidate.address,
                location_provenance={'venue_name':'event_specific','address':'event_specific'})
            enrich_missing_location(canonical,suggestion)
            if canonical.price is None and canonical.price_status == 'unknown' and candidate.price is not None:
                from .extraction import enrich_candidate_pricing
                if enrich_candidate_pricing(canonical,candidate.price):
                    canonical.extraction_notes += f'\n[Source enrichment] Pricing from RawSourceItem #{candidate.raw_source_item_id}.'
                    canonical.save(update_fields=['extraction_notes','updated_at'])
        return canonical,False
    candidate.save()
    EventSourceObservation.objects.create(raw_source_item=candidate.raw_source_item,fingerprint=fingerprint,candidate=candidate,
        match_status=level or 'unique',matched_candidate=other if isinstance(other,EventCandidate) else None,
        matched_event=other if isinstance(other,Event) else None,facts=facts)
    if level=='probable':
        candidate.extraction_notes += f'\n[Duplicate] Probable match: {other._meta.label} #{other.pk}; compare source observations before approval.'
        candidate.save(update_fields=['extraction_notes','updated_at'])
    return candidate,True


def match_diagnostics(candidate):
    other,level=find_match(candidate)
    if other is None: return {'decision':'unique'}
    delta=abs((candidate.start_time-other.start_time).total_seconds())/60
    source=str(other.source_account) if isinstance(other,EventCandidate) else 'existing public Event'
    other_lat,other_lon=getattr(other,'latitude',None),getattr(other,'longitude',None)
    if isinstance(other,Event) and other.location:
        other_lat,other_lon=other.location.y,other.location.x
    distance=None
    if all(value is not None for value in (candidate.latitude,candidate.longitude,other_lat,other_lon)):
        distance=round((((candidate.latitude-other_lat)*111000)**2+((candidate.longitude-other_lon)*72000)**2)**.5,1)
    return {'geographic_distance_meters':distance,'decision':level,'match':f'{other._meta.label} #{other.pk}: {other.title}',
        'existing_source':source,'title_similarity':round(title_similarity(candidate.title,other.title),3),
        'date_match':candidate.start_time.astimezone(ZoneInfo('Europe/Kyiv')).date()==other.start_time.astimezone(ZoneInfo('Europe/Kyiv')).date(),
        'time_difference_minutes':round(delta,2),'venue_match':bool(getattr(other,'venue_name','') and normalize_fact(candidate.venue_name)==normalize_fact(other.venue_name))}
