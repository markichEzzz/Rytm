from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import Event, UserEventInteraction

PREFERENCES = ('interested', 'not_interested')
WEAK_REFRESH_INTERVAL = timedelta(hours=24)

def interest_states(user, event_ids=None):
    rows=UserEventInteraction.objects.filter(user=user, interaction_type__in=PREFERENCES)
    if event_ids is not None: rows=rows.filter(event_id__in=event_ids)
    return dict(rows.values_list('event_id', 'interaction_type'))

class InteractionInput(serializers.Serializer):
    type=serializers.ChoiceField(choices=UserEventInteraction.Type.choices)
    active=serializers.BooleanField(default=True)
    def validate(self, attrs):
        if not attrs['active'] and attrs['type'] not in PREFERENCES:
            raise serializers.ValidationError('Only explicit preferences can be withdrawn.')
        return attrs

class EventInteractionView(APIView):
    permission_classes=[IsAuthenticated]
    def finalize_response(self, request, response, *args, **kwargs):
        response=super().finalize_response(request,response,*args,**kwargs)
        response['Cache-Control']='private, no-store'
        return response

    def get(self, request, pk):
        event=get_object_or_404(Event, pk=pk, status='approved')
        return Response({'user_interest':interest_states(request.user,[event.pk]).get(event.pk,'neutral')})

    def post(self, request, pk):
        event=get_object_or_404(Event, pk=pk, status='approved')
        data=InteractionInput(data=request.data);data.is_valid(raise_exception=True)
        kind=data.validated_data['type'];active=data.validated_data['active'];now=timezone.now()
        with transaction.atomic():
            # Serialize preference swaps for this actual authenticated user, including concurrent taps.
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            rows=UserEventInteraction.objects.filter(user=request.user,event=event)
            if not active:
                rows.filter(interaction_type=kind).delete()
            else:
                if kind in PREFERENCES: rows.filter(interaction_type__in=PREFERENCES).exclude(interaction_type=kind).delete()
                interaction,created=UserEventInteraction.objects.get_or_create(user=request.user,event=event,interaction_type=kind,
                    defaults={'created_at':now,'last_interacted_at':now})
                if not created and interaction.last_interacted_at <= now-WEAK_REFRESH_INTERVAL:
                    interaction.last_interacted_at=now;interaction.save(update_fields=['last_interacted_at'])
            state=interest_states(request.user,[event.pk]).get(event.pk,'neutral')
        return Response({'user_interest':state})
