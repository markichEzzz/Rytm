from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from django.contrib.gis.geos import Polygon
from django.db.models import Q
from django.utils import timezone

from .models import Event
from .serializers import EventSerializer
from .moderation import create_submission
from .search import filter_public_events


class PrivateEventStateMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        from django.utils.cache import patch_vary_headers
        response=super().finalize_response(request,response,*args,**kwargs)
        patch_vary_headers(response,('Authorization','Cookie'))
        if request.user.is_authenticated: response['Cache-Control']='private, no-store'
        return response

class EventListCreateView(PrivateEventStateMixin, generics.ListCreateAPIView):
    serializer_class = EventSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        now = timezone.now()

        # Approved + not expired: use end_time if it exists, otherwise start_time.
        queryset = (
            Event.objects
            .filter(status='approved')
            .filter(
                Q(end_time__gte=now) | Q(end_time__isnull=True, start_time__gte=now)
            )
            .order_by('start_time')
        )

        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        bbox = self.request.query_params.get('bbox')
        if bbox:
            try:
                coords = [float(c) for c in bbox.split(',')]

                if len(coords) == 4:
                    geom = Polygon.from_bbox(coords)
                    queryset = queryset.filter(location__within=geom)

            except (ValueError, TypeError):
                pass

        return filter_public_events(queryset, self.request.query_params)

    def list(self, request, *args, **kwargs):
        from rest_framework.response import Response
        from rest_framework.exceptions import ValidationError
        from accounts.reputation import reputation_summaries
        from .interactions import interest_states
        from .recommendations import rank_events
        ordering=request.query_params.get('ordering','all')
        if ordering not in ('all','for_you','start_time',''):
            raise ValidationError({'ordering':'Use all or for_you.'})
        events=list(self.filter_queryset(self.get_queryset()).select_related('submitted_by__profile','ingestion_candidate'))
        context=self.get_serializer_context()
        context['_reputation']=reputation_summaries(event.submitted_by_id for event in events if event.submitted_by_id)
        if request.user.is_authenticated:
            states=interest_states(request.user,[event.pk for event in events])
            context['_interest_by_event']=states
            context['_include_interest']=True
            if ordering=='for_you': events=rank_events(events,request.user,states)
        return Response(self.get_serializer(events,many=True,context=context).data)

    def perform_create(self, serializer):
        create_submission(serializer, self.request.user)


class EventDetailView(PrivateEventStateMixin, generics.RetrieveAPIView):
    def get_serializer_context(self):
        context=super().get_serializer_context()
        context['_include_interest']=self.request.user.is_authenticated
        return context

    serializer_class = EventSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return Event.objects.filter(status='approved').select_related('submitted_by__profile','ingestion_candidate')
