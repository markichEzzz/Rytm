"""Validated, composable public-field search and Kyiv start-date boundaries."""
from datetime import datetime, time, timedelta
from datetime import date
from zoneinfo import ZoneInfo
from django.db.models import Q
from rest_framework import serializers
from .pricing import PriceStatus


class SearchParams(serializers.Serializer):
    q = serializers.CharField(required=False,allow_blank=True,max_length=200,trim_whitespace=True)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    price_status = serializers.ChoiceField(choices=PriceStatus.choices,required=False)

    def validate_date_to(self,value):
        if value == date.max:
            raise serializers.ValidationError('Date is outside the supported range.')
        return value

    def validate(self,attrs):
        if attrs.get('date_from') and attrs.get('date_to') and attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError('date_from must not be after date_to.')
        return attrs


def filter_public_events(queryset,params):
    serializer=SearchParams(data=params)
    serializer.is_valid(raise_exception=True)
    values=serializer.validated_data
    if values.get('q'):
        q=values['q']
        queryset=queryset.filter(Q(title__icontains=q)|Q(description__icontains=q)|Q(address__icontains=q))
    if values.get('price_status'):
        queryset=queryset.filter(price_status=values['price_status'])
    zone=ZoneInfo('Europe/Kyiv')
    if values.get('date_from'):
        queryset=queryset.filter(start_time__gte=datetime.combine(values['date_from'],time.min,tzinfo=zone))
    if values.get('date_to'):
        queryset=queryset.filter(start_time__lt=datetime.combine(values['date_to']+timedelta(days=1),time.min,tzinfo=zone))
    return queryset
