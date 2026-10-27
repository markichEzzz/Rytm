"""Derived trust; only proven ownership and reviewed reports are counted."""
from django.db.models import Count, Q
from events.models import Event, EventReport, Organizer
from ingestion.models import EventCandidate

LABELS = ((30, 'Низька надійність'), (60, 'Новий організатор'),
          (80, 'Надійний організатор'), (101, 'Висока надійність'))

def reputation_label(score):
    return next(label for ceiling, label in LABELS if score < ceiling)

def reputation_summaries(user_ids):
    user_ids=set(user_ids)
    if not user_ids: return {}
    events={row['submitted_by_id']:row for row in Event.objects.filter(submitted_by_id__in=user_ids).values('submitted_by_id').annotate(
        approved=Count('pk',filter=Q(status='approved')),pending=Count('pk',filter=Q(status='pending')),rejected=Count('pk',filter=Q(status='rejected')))}
    candidates={row['submitted_by_id']:row for row in EventCandidate.objects.filter(submitted_by_id__in=user_ids,event__isnull=True).values('submitted_by_id').annotate(
        pending=Count('pk',filter=Q(status='new')),rejected=Count('pk',filter=Q(status__in=['rejected','duplicate'])))}
    complaints=dict(EventReport.objects.filter(event__submitted_by_id__in=user_ids,status='confirmed').values('event__submitted_by_id').annotate(total=Count('pk')).values_list('event__submitted_by_id','total'))
    verified=set(Organizer.objects.filter(user_id__in=user_ids,verified=True).values_list('user_id',flat=True))
    result={}
    for user_id in user_ids:
        event=events.get(user_id,{});candidate=candidates.get(user_id,{})
        stats=dict(approved_events=event.get('approved',0),pending_events=event.get('pending',0)+candidate.get('pending',0),
            rejected_events=event.get('rejected',0)+candidate.get('rejected',0),confirmed_complaints=complaints.get(user_id,0))
        score=max(0,min(100,50+2*stats['approved_events']-5*stats['rejected_events']-10*stats['confirmed_complaints']))
        result[user_id]={'reputation':{'score':score,'label':reputation_label(score)},'stats':stats,'verified_organizer':user_id in verified}
    return result

def reputation_summary(user_id):
    return reputation_summaries([user_id])[user_id]

def is_verified(user_id):
    return Organizer.objects.filter(user_id=user_id, verified=True).exists()
