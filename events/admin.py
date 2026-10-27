from django.contrib.gis import admin
from .models import Organizer, Event

@admin.register(Organizer)
class OrganizerAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'contact_email', 'verified')
    autocomplete_fields = ('user',)
    list_filter = ('verified',)
    search_fields = ('name', 'contact_email')

@admin.register(Event)
class EventAdmin(admin.GISModelAdmin):
    list_display = ('title', 'category', 'start_time', 'status', 'organizer', 'submitted_by', 'created_at')
    readonly_fields = ('submitted_by', 'created_at')
    actions = ('approve_selected', 'mark_pending', 'reject_selected')
    list_filter = ('status', 'category')
    search_fields = ('title', 'description', 'address')
    # gis_widget_kwargs налаштовує початковий вигляд карти (можна налаштувати на Львів)
    default_lon = 24.0297  # Довгота Львова
    default_lat = 49.8397  # Широта Львова
    default_zoom = 12

    def set_status(self, request, queryset, status):
        # Save individually so the existing notification signal still runs.
        count = 0
        for event in queryset.exclude(status=status):
            event.status = status
            event.save(update_fields=['status'])
            count += 1
        self.message_user(request, f'Updated {count} events.')

    @admin.action(description='Approve selected events')
    def approve_selected(self, request, queryset):
        self.set_status(request, queryset, 'approved')

    @admin.action(description='Mark selected events as pending')
    def mark_pending(self, request, queryset):
        self.set_status(request, queryset, 'pending')

    @admin.action(description='Reject selected events')
    def reject_selected(self, request, queryset):
        self.set_status(request, queryset, 'rejected')


from django.utils import timezone
from django import forms
from .models import EventReport

class EventReportAdminForm(forms.ModelForm):
    class Meta:
        model = EventReport
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        if (self.instance.pk and self.instance.reporter_id is not None and cleaned.get('status') != 'dismissed'
                and EventReport.objects.filter(reporter_id=self.instance.reporter_id,
                    event_id=self.instance.event_id).exclude(pk=self.instance.pk)
                    .exclude(status='dismissed').exists()):
            self.add_error('status', 'Для цього користувача вже є активна скарга на цю подію.')
        return cleaned


@admin.register(EventReport)
class EventReportAdmin(admin.ModelAdmin):
    form = EventReportAdminForm
    list_display = ('id', 'event', 'event_owner', 'reporter', 'reason', 'status', 'created_at', 'reviewed_by')
    list_filter = ('status', 'reason', 'created_at')
    search_fields = ('event__title', 'reporter__email', 'description')
    readonly_fields = ('event', 'event_owner', 'reporter', 'reason', 'description', 'created_at', 'reviewed_at', 'reviewed_by')
    actions = ('confirm_reports', 'dismiss_reports', 'review_reports')
    list_select_related = ('event__submitted_by', 'reporter', 'reviewed_by')

    @admin.display(description='Власник події')
    def event_owner(self, report):
        return report.event.submitted_by

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if 'status' in form.changed_data:
            obj.reviewed_at = timezone.now()
            obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)

    def set_status(self, request, queryset, status):
        queryset.exclude(status=status).update(status=status, reviewed_at=timezone.now(), reviewed_by=request.user)

    @admin.action(description='Підтвердити скарги', permissions=['change'])
    def confirm_reports(self, request, queryset):
        self.set_status(request, queryset.exclude(status='dismissed'), 'confirmed')

    @admin.action(description='Відхилити скарги', permissions=['change'])
    def dismiss_reports(self, request, queryset):
        self.set_status(request, queryset, 'dismissed')

    @admin.action(description='Взяти на розгляд', permissions=['change'])
    def review_reports(self, request, queryset):
        self.set_status(request, queryset.exclude(status='dismissed'), 'under_review')
