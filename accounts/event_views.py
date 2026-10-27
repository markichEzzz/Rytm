from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from events.models import Event
from events.serializers import EventSerializer
from ingestion.models import EventCandidate


def filter_period(queryset, period):
    now = timezone.now()
    upcoming = Q(end_time__gte=now) | Q(end_time__isnull=True, start_time__gte=now)
    if period == 'upcoming':
        return queryset.filter(upcoming)
    if period == 'past':
        return queryset.exclude(upcoming)
    if period not in ('', None, 'all'):
        raise ValidationError({'period': 'Use upcoming, past or all.'})
    return queryset


class OwnSubmissionsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        candidates = EventCandidate.objects.filter(submitted_by=request.user).select_related('event')
        records = []
        linked = []
        for candidate in candidates:
            if candidate.event_id:
                linked.append(candidate.event_id)
            status = {'new': 'pending', 'duplicate': 'rejected'}.get(candidate.status, candidate.status)
            if candidate.event_id and candidate.event.status != 'pending':
                status = candidate.event.status
            records.append({'id': candidate.pk, 'kind': 'candidate', 'title': candidate.title,
                'start_time': candidate.start_time, 'end_time': candidate.end_time,
                'venue': candidate.venue_name or candidate.address, 'category': candidate.category,
                'status': status, 'created_at': candidate.created_at,
                'public_event_id': candidate.event_id if candidate.event_id and candidate.event.status == 'approved' else None})
        # Historical owned Events need no speculative candidate/owner backfill.
        for event in Event.objects.filter(submitted_by=request.user).exclude(pk__in=linked):
            records.append({'id': event.pk, 'kind': 'event', 'title': event.title,
                'start_time': event.start_time, 'end_time': event.end_time,
                'venue': event.address, 'category': event.category, 'status': event.status,
                'created_at': event.created_at, 'public_event_id': event.pk if event.status == 'approved' else None})
        return Response(sorted(records, key=lambda row: row['created_at'], reverse=True))

class OwnEventsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = EventSerializer
    def get_queryset(self):
        return filter_period(Event.objects.filter(submitted_by=self.request.user, status='approved'),
                             self.request.query_params.get('period')).order_by('start_time', 'pk')

class PublicUserEventsView(OwnEventsView):
    permission_classes = [AllowAny]
    def get_queryset(self):
        user = get_object_or_404(get_user_model(), pk=self.kwargs['user_id'], is_active=True)
        return filter_period(Event.objects.filter(submitted_by=user, status='approved'),
                             self.request.query_params.get('period')).order_by('start_time', 'pk')
