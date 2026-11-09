from django import forms
from events.moderation import LVIV_BOUNDS
from .models import EventCandidate
from events.pricing import normalize_price
from django.core.exceptions import ValidationError


class EventCandidateAdminForm(forms.ModelForm):
    confirm_extraction_review = forms.BooleanField(
        required=False, label='I checked the extraction ambiguities against the source',
        help_text='Confirm only after correcting or verifying the warnings shown above. Required publication fields still apply.',
    )

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
        if 'public_description' in self.fields:
            from .services import final_candidate_description
            self.fields['public_description'].label = 'Final public description'
            self.fields['public_description'].widget = forms.Textarea(attrs={'rows': 7, 'cols': 90})
            self.initial.setdefault('public_description', '')
            if not self.instance.public_description:
                self.initial['public_description'] = final_candidate_description(self.instance)
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
        # Display generated copy as a starting point without freezing it when the
        # moderator only changes facts. Only an actual text edit stores an override.
        if ('public_description' in cleaned and not self.instance.public_description
                and cleaned['public_description'] == self.initial.get('public_description', '').strip()):
            cleaned['public_description'] = ''
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
