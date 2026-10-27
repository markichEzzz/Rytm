from django import forms
from events.moderation import LVIV_BOUNDS
from .models import EventCandidate
from events.pricing import normalize_price
from django.core.exceptions import ValidationError


class EventCandidateAdminForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        data = args[0] if args else kwargs.get('data')
        if data is not None and data.get('price_status') in ('free', 'unknown'):
            data = data.copy()
            data['price'] = '0.00' if data['price_status'] == 'free' else ''
            if args:
                args = (data, *args[1:])
            else:
                kwargs['data'] = data
        super().__init__(*args, **kwargs)
        if 'public_photo' in self.fields:
            self.fields['public_photo'].help_text = (
                'Upload a photo you own or have permission to publish, or authorize the source image below. '
                'A manual upload takes priority.'
            )

    class Meta:
        model = EventCandidate
        fields = '__all__'

    class Media:
        css = {'all': ('ingestion/vendor/leaflet/leaflet.css', 'ingestion/admin/candidate.css')}
        js = ('ingestion/vendor/leaflet/leaflet.js', 'ingestion/admin/candidate.js')

    def clean(self):
        cleaned = super().clean()
        if 'price_status' in cleaned:
            try:
                cleaned['price'] = normalize_price(cleaned['price_status'], cleaned.get('price'))
            except ValidationError as error:
                self.add_error('price', error.message_dict['price'])
        lat, lon = cleaned.get('latitude'), cleaned.get('longitude')
        if lat is not None and lon is not None:
            min_lon, min_lat, max_lon, max_lat = LVIV_BOUNDS
            if not min_lat <= lat <= max_lat or not min_lon <= lon <= max_lon:
                self.add_error('latitude', 'Select a location within the supported Lviv bounds.')
        return cleaned
