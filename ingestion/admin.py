import json

from django.contrib import admin, messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse
from django.utils import timezone
from .forms import EventCandidateAdminForm
from .public_images import source_image_url
from .geocoding import resolve_candidate_location, location_note
from django.utils.html import format_html, format_html_join
from urllib.parse import urlencode

from .extraction import configured_provider, extract_raw_source_item

from .models import SourceAccount, RawSourceItem, EventCandidate, EventSourceObservation
from .services import approve_candidate, review_candidate, build_candidate_description, find_possible_duplicate


@admin.register(SourceAccount)
class SourceAccountAdmin(admin.ModelAdmin):
    list_display = ('username', 'platform', 'display_name', 'enabled', 'last_checked_at')
    list_filter = ('platform', 'enabled')
    list_editable = ('enabled',)
    search_fields = ('username', 'display_name')
    readonly_fields = ('created_at', 'updated_at', 'last_checked_at')


@admin.register(RawSourceItem)
class RawSourceItemAdmin(admin.ModelAdmin):
    list_display = ('source_account', 'external_id', 'source_url', 'published_at', 'fetched_at', 'extraction_status', 'raw_text_preview', 'candidate_link')
    list_filter = ('source_account', 'extraction_status')
    search_fields = ('external_id', 'source_account__username', 'raw_text')
    autocomplete_fields = ('source_account',)
    readonly_fields = ('created_at', 'updated_at', 'candidate_link', 'image_text', 'semantic_extraction', 'poster_diagnostics')
    actions = ('run_extraction',)

    @admin.display(description='Image text/OCR (private)')
    def image_text(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', (obj.raw_metadata or {}).get('image_text', '') if obj else '')

    @admin.display(description='Semantic extraction/evidence (private)')
    def semantic_extraction(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', json.dumps((obj.raw_metadata or {}).get('event_extraction', {}), ensure_ascii=False, indent=2) if obj else '')

    @admin.display(description='Poster groups and ambiguity (private)')
    def poster_diagnostics(self, obj):
        metadata = (obj.raw_metadata or {}) if obj else {}
        data = {'layout': metadata.get('poster_layout', {}),
                'events': metadata.get('event_extraction', {}).get('poster_events', []),
                'split_review': metadata.get('poster_split_review')}
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', json.dumps(data, ensure_ascii=False, indent=2))

    @admin.display(description='Candidate')
    def candidate_link(self, obj):
        count = obj.candidates.count() if obj and obj.pk else 0
        if not count:
            if obj and obj.pk and obj.observations.exists():
                url = reverse('admin:ingestion_eventsourceobservation_changelist') + '?' + urlencode({'raw_source_item__id__exact': obj.pk})
                return format_html('<a href="{}">View linked observations</a>', url)
            return '—'
        url = reverse('admin:ingestion_eventcandidate_changelist') + '?' + urlencode({'raw_source_item__id__exact': obj.pk})
        return format_html('<a href="{}">View candidates ({})</a>', url, count)

    @admin.action(description='Run extraction (new/failed items)', permissions=['extract'])
    def run_extraction(self, request, queryset):
        if not self.has_extract_permission(request):
            raise PermissionDenied('Raw item change and candidate creation permissions are required.')
        provider = configured_provider()
        counts = {}
        for item in queryset.select_related('source_account'):
            outcome = extract_raw_source_item(item, provider=provider, retry_failed=True, reextract=True)
            counts[outcome.action] = counts.get(outcome.action, 0) + 1
        self.message_user(request, 'Extraction results: ' + ', '.join(f'{key}: {value}' for key, value in sorted(counts.items())),
                          level=messages.WARNING if counts.get('failed') else messages.INFO)

    def has_extract_permission(self, request):
        return (request.user.has_perm('ingestion.change_rawsourceitem')
                and request.user.has_perm('ingestion.add_eventcandidate'))

    @admin.display(description='Raw text')
    def raw_text_preview(self, obj):
        text = ' '.join(obj.raw_text.split())
        return text[:120] + ('…' if len(text) > 120 else '')

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj:
            fields += ('source_account', 'external_id', 'source_url', 'published_at',
                       'raw_text', 'raw_image_url', 'raw_metadata', 'fetched_at')
        return fields


class ObservationInline(admin.TabularInline):
    model = EventSourceObservation
    fk_name = 'candidate'
    extra = 0
    can_delete = False
    fields = ('raw_source_item','match_status','facts','matched_candidate','matched_event','event')
    readonly_fields = fields

    def has_add_permission(self,request,obj=None):
        return False


@admin.register(EventCandidate)
class EventCandidateAdmin(admin.ModelAdmin):
    inlines = [ObservationInline]

    @admin.display(description="Duplicate state")
    def duplicate_state(self,obj):
        if obj.status == "duplicate":
            return "Confirmed by moderator"
        if obj.observations.filter(match_status="probable").exists():
            return "Probable duplicate — compare linked observations"
        if obj.observations.filter(match_status="confirmed").exists():
            return "Linked sources / confirmed match"
        return "Unique"

    form = EventCandidateAdminForm
    fieldsets = (
        ('Event facts', {'fields': ('title', 'description', 'start_time', 'end_time', 'category', 'price_status', 'price')}),
        ('Location', {'fields': ('venue_name', 'address', 'address_provenance', 'location_provenance', 'location_picker', ('latitude', 'longitude'))}),
        ('Publication photo', {'fields': ('source_image_preview', 'public_photo', 'use_source_image_as_public_photo',
             'source_photo_authorized_by', 'source_photo_authorized_at')}),
        ('Review', {'fields': ('submitted_by', 'confidence', 'extraction_notes', 'status', 'event', 'reviewed_by', 'reviewed_at', 'possible_duplicate', 'public_description')}),
        ('Source reference (private)', {'classes': ('collapse',), 'fields': ('raw_source_item', 'source_account', 'source_post',
             'source_fingerprint', 'sibling_candidates', 'source_url', 'source_image_url', 'raw_source_text', 'raw_image_text', 'semantic_extraction', 'poster_diagnostics', 'created_at', 'updated_at')}),
    )

    @admin.display(description='Select location')
    def location_picker(self, obj):
        return format_html('<div id="candidate-location-map" data-latitude="{}" data-longitude="{}" aria-label="Select event location on map"></div><p>Click to place a marker or drag it. Coordinates must be within Lviv.</p>',
            obj.latitude if obj and obj.latitude is not None else '', obj.longitude if obj and obj.longitude is not None else '')

    @admin.display(description='Source image')
    def source_image_preview(self, obj):
        url = source_image_url(obj) if obj and obj.raw_source_item_id else ''
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            safe = parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443)
        except ValueError:
            safe = False
        if not safe:
            return 'No source image available. Upload a public photo manually.'
        return format_html('<div class="source-image-preview"><a href="{}" target="_blank" rel="noopener noreferrer"><img class="candidate-source-image" src="{}" referrerpolicy="no-referrer" alt="Private source poster"></a><p class="source-image-error" hidden>Source image unavailable or expired. Upload a public photo manually.</p><p><a href="{}" target="_blank" rel="noopener noreferrer">Open source image</a></p></div>', url, url, url)

    def save_model(self, request, obj, form, change):
        candidate_fields = {'title', 'start_time', 'end_time', 'category', 'description'} & set(form.changed_data)
        if candidate_fields:
            obj.extraction_notes = (obj.extraction_notes + '\n[Candidate facts] Moderator edited: ' + ','.join(sorted(candidate_fields))).strip()
        location_fields = {'venue_name', 'address'} & set(form.changed_data)
        if location_fields:
            obj.extraction_notes = (obj.extraction_notes + '\n[Location facts] Moderator edited: ' + ','.join(sorted(location_fields))).strip()
            location_note(obj, 'Venue/address edited; existing coordinates require manual verification.')
        if 'address' in form.changed_data:
            obj.extraction_notes = (obj.extraction_notes + '\n[Address] moderator: edited in admin.').strip()
        price_fields = {'price', 'price_status'} & set(form.changed_data)
        if price_fields:
            obj.extraction_notes = (obj.extraction_notes + '\n[Pricing] Moderator edited: ' + ','.join(sorted(price_fields))).strip()
        if {'latitude', 'longitude'} & set(form.changed_data):
            location_note(obj, 'Moderator selected or edited coordinates.')
        if 'use_source_image_as_public_photo' in form.changed_data:
            obj.source_photo_authorized_by = request.user if obj.use_source_image_as_public_photo else None
            obj.source_photo_authorized_at = timezone.now() if obj.use_source_image_as_public_photo else None
        super().save_model(request, obj, form, change)

    list_display = ('title', 'submitted_by', 'duplicate_state', 'source_account', 'start_time', 'venue_name', 'address', 'category',
                    'price_status', 'price', 'confidence', 'status', 'source_url', 'created_at')
    list_filter = ('status', 'raw_source_item__source_account', 'category', 'start_time')
    search_fields = ('title', 'address', 'raw_source_item__source_account__username')
    autocomplete_fields = ('raw_source_item',)
    readonly_fields = ('submitted_by', 'status', 'event', 'reviewed_at', 'reviewed_by', 'created_at', 'updated_at',
                       'source_photo_authorized_at', 'source_photo_authorized_by', 'address_provenance', 'location_provenance', 'location_picker', 'source_image_preview', 'source_account', 'source_fingerprint', 'source_post', 'sibling_candidates', 'raw_source_text', 'raw_image_text', 'semantic_extraction', 'poster_diagnostics', 'public_description', 'possible_duplicate')
    actions = ('resolve_location', 'approve_selected', 'reject_selected', 'mark_duplicate', 'return_to_review')

    @admin.display(description='Location source')
    def location_provenance(self, obj):
        notes = [line.removeprefix('[Location] ') for line in (obj.extraction_notes if obj else '').splitlines() if line.startswith('[Location] ')]
        return (notes[-1] if notes else 'Existing/extracted coordinates; verify manually.' if obj and obj.latitude is not None and obj.longitude is not None else 'Unresolved; use Resolve location or select on the map.') + ' Geocoding data © OpenStreetMap contributors (ODbL).'

    @admin.display(description='Address source (private)')
    def address_provenance(self, obj):
        from .provenance import address_source
        return address_source(obj) or ('Existing address; verify manually.' if obj and obj.address else 'Unresolved.') if obj else 'Unresolved.'

    @admin.action(description='Resolve location from venue/address (missing coordinates only)')
    def resolve_location(self, request, queryset):
        for candidate in queryset:
            try:
                self.message_user(request, f'Candidate #{candidate.pk}: {resolve_candidate_location(candidate, request.user)}')
            except PermissionDenied as error:
                self.message_user(request, str(error), level=messages.WARNING)
    list_select_related = ('raw_source_item__source_account',)

    @admin.display(description='Source post')
    def source_post(self, obj):
        return format_html('<a href="{}">{}</a>', obj.source_url, obj.source_url) if obj else ''

    @admin.display(description='Candidates from this post')
    def sibling_candidates(self, obj):
        if not obj or not obj.raw_source_item_id:
            return '—'
        return format_html_join('<br>', '<a href="{}">{}</a>', ((reverse('admin:ingestion_eventcandidate_change', args=[item.pk]), item.title or f'Candidate #{item.pk}') for item in obj.raw_source_item.candidates.order_by('start_time', 'pk')))

    @admin.display(description='Raw source text (internal reference)')
    def raw_source_text(self, obj):
        return obj.raw_source_item.raw_text if obj and obj.raw_source_item_id else ''

    @admin.display(description='Image text/OCR (private reference)')
    def raw_image_text(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', (obj.raw_source_item.raw_metadata or {}).get('image_text', '') if obj and obj.raw_source_item_id else '')

    @admin.display(description='Semantic extraction/evidence (private)')
    def semantic_extraction(self, obj):
        return RawSourceItemAdmin(RawSourceItem, self.admin_site).semantic_extraction(obj.raw_source_item) if obj and obj.raw_source_item_id else ''

    @admin.display(description='Poster groups and ambiguity (private)')
    def poster_diagnostics(self, obj):
        return RawSourceItemAdmin(RawSourceItem, self.admin_site).poster_diagnostics(obj.raw_source_item) if obj and obj.raw_source_item_id else '—'

    @admin.display(description='Public description generated from facts')
    def public_description(self, obj):
        return (build_candidate_description(obj) if obj.raw_source_item_id else obj.description) if obj else ''

    @admin.display(description='Possible duplicate')
    def possible_duplicate(self, obj):
        if not obj or obj.event_id:
            return '—'
        return find_possible_duplicate(obj) or 'No match found'

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.status == EventCandidate.Status.APPROVED:
            fields += ('raw_source_item', 'title', 'description', 'start_time', 'end_time', 'venue_name',
                       'address', 'latitude', 'longitude', 'price', 'price_status', 'category', 'source_url',
                       'source_image_url', 'public_photo', 'use_source_image_as_public_photo', 'confidence', 'extraction_notes')
        return fields

    def moderate(self, request, queryset, status):
        count = 0
        for candidate in queryset:
            try:
                if status == EventCandidate.Status.APPROVED:
                    previous_status = candidate.status
                    _, created = approve_candidate(candidate, request.user)
                    candidate.refresh_from_db(fields=['status'])
                    count += int(created or candidate.status != previous_status)
                else:
                    review_candidate(candidate, request.user, status)
                    count += 1
            except (ValidationError, PermissionDenied) as error:
                detail = '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)
                self.message_user(request, f'Candidate #{candidate.pk}: {detail}', level=messages.WARNING)
        self.message_user(request, f'{count} candidates updated. See warnings for skipped items.')

    @admin.action(description='Approve selected candidates')
    def approve_selected(self, request, queryset):
        self.moderate(request, queryset, EventCandidate.Status.APPROVED)

    @admin.action(description='Reject selected candidates')
    def reject_selected(self, request, queryset):
        self.moderate(request, queryset, EventCandidate.Status.REJECTED)

    @admin.action(description='Mark selected candidates as duplicate')
    def mark_duplicate(self, request, queryset):
        self.moderate(request, queryset, EventCandidate.Status.DUPLICATE)

    @admin.action(description='Return rejected/duplicate candidates to review')
    def return_to_review(self, request, queryset):
        self.moderate(request, queryset, EventCandidate.Status.NEW)


@admin.register(EventSourceObservation)
class EventSourceObservationAdmin(admin.ModelAdmin):
    list_display = ('raw_source_item','candidate','event','match_status','matched_candidate','matched_event')
    list_filter = ('match_status','raw_source_item__source_account__platform')
    readonly_fields = ('raw_source_item','fingerprint','candidate','event','match_status','matched_candidate','matched_event','facts','created_at')

    def has_add_permission(self,request):
        return False

    def has_delete_permission(self,request,obj=None):
        return False
