"""Deterministic ranking of an already visibility/filter-constrained catalogue."""
from collections import Counter, defaultdict
from datetime import timedelta
from math import sqrt
from django.db.models import Count, F, Q
from django.utils import timezone
from .models import UserEventInteraction

HISTORY_DAYS=90
SIGNAL_WEIGHTS={'view':1.0,'interested':6.0,'map_open':2.0,'organizer_open':2.0,'not_interested':-6.0}
EVENT_SIGNAL_CAP=8.0
AFFINITY_CAP=12.0
CATEGORY_WEIGHT=.25
ORGANIZER_WEIGHT=.10
SAVED_BOOST=3.0
DISLIKED_PENALTY=6.0
RECENCY_WEIGHT=4.0
RECENCY_DAYS=7.0
FUTURE_DECAY_DAYS=30.0
QUALITY_WEIGHT=.5
POPULARITY_WEIGHT=.25
POPULARITY_CAP=1.5
EXPLORATION_INTERVAL=5

def clamp(value, cap): return max(-cap,min(cap,value))

def affinities(user, now):
    rows=UserEventInteraction.objects.filter(user=user,last_interacted_at__gte=now-timedelta(days=HISTORY_DAYS),event__status='approved').values(
        'event_id','interaction_type','event__category','event__submitted_by_id','event__submitted_by__is_active')
    by_event=defaultdict(list)
    for row in rows: by_event[row['event_id']].append(row)
    categories=defaultdict(float);organizers=defaultdict(float)
    for signals in by_event.values():
        sample=signals[0]
        kinds={row['interaction_type'] for row in signals}
        weight=SIGNAL_WEIGHTS['not_interested'] if 'not_interested' in kinds else min(EVENT_SIGNAL_CAP,sum(SIGNAL_WEIGHTS[k] for k in kinds))
        categories[sample['event__category']]+=weight
        if sample['event__submitted_by_id'] and sample['event__submitted_by__is_active']:
            organizers[sample['event__submitted_by_id']]+=weight
    return categories,organizers,bool(by_event)

def popularity(events, now):
    rows=UserEventInteraction.objects.filter(event_id__in=[event.pk for event in events],interaction_type='interested',user__is_active=True,
        last_interacted_at__gte=now-timedelta(days=HISTORY_DAYS)).filter(Q(event__submitted_by__isnull=True)|~Q(user_id=F('event__submitted_by_id')))
    return dict(rows.values('event_id').annotate(total=Count('user_id',distinct=True)).values_list('event_id','total'))

def event_score(event, categories, organizers, states, popular, now):
    days=max(0,(event.start_time-now).total_seconds()/86400)
    future=1/(1+days/FUTURE_DECAY_DAYS)
    category=CATEGORY_WEIGHT*clamp(categories.get(event.category,0),AFFINITY_CAP)
    organizer=ORGANIZER_WEIGHT*clamp(organizers.get(event.submitted_by_id,0),AFFINITY_CAP) if event.submitted_by_id else 0
    preference=states.get(event.pk,'neutral')
    quality=sum(bool(value) for value in (event.description.strip(),event.address.strip(),event.photo.name))/3
    popular_score=min(POPULARITY_CAP,POPULARITY_WEIGHT*sqrt(popular.get(event.pk,0)))
    return (RECENCY_WEIGHT/(1+days/RECENCY_DAYS) + QUALITY_WEIGHT*quality
        + future*(category+organizer+(SAVED_BOOST if preference=='interested' else 0)+popular_score)
        - (DISLIKED_PENALTY if preference=='not_interested' else 0))

def rank_events(events, user, states, now=None):
    now=now or timezone.now()
    categories,organizers,has_history=affinities(user,now)
    popular=popularity(events,now)
    scores={event.pk:event_score(event,categories,organizers,states,popular,now) for event in events}
    remaining=sorted(events,key=lambda event:(-scores[event.pk],event.start_time,event.pk))
    if not has_history: return remaining
    result=[];counts=Counter()
    while remaining:
        if (len(result)+1)%EXPLORATION_INTERVAL==0:
            pool=[event for event in remaining if states.get(event.pk)!='not_interested'] or remaining
            choice=min(pool,key=lambda event:(counts[event.category],event.start_time,event.pk))
            remaining.remove(choice)
        else: choice=remaining.pop(0)
        result.append(choice);counts[choice.category]+=1
    return result
