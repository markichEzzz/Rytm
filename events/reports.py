from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import Event, EventReport


class ReportInputSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=EventReport.Reason.choices)
    description = serializers.CharField(max_length=2000, allow_blank=True, required=False, default='')


class EventReportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        event = get_object_or_404(Event, pk=pk, status='approved')
        serializer = ReportInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                report = EventReport.objects.create(event=event, reporter=request.user, **serializer.validated_data)
        except IntegrityError:
            if EventReport.objects.filter(event=event, reporter=request.user).exclude(status='dismissed').exists():
                return Response({'code': 'duplicate_report', 'detail': 'Ви вже надіслали скаргу на цю подію. Вона доступна модераторам.'}, status=409)
            raise
        return Response({'id': report.pk, 'status': report.status, 'created_at': report.created_at}, status=201)
