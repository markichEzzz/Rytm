from django.contrib.gis import admin
from .models import Organizer, Event

@admin.register(Organizer)
class OrganizerAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_email', 'verified')
    list_filter = ('verified',)
    search_fields = ('name', 'contact_email')

@admin.register(Event)
class EventAdmin(admin.GISModelAdmin):
    list_display = ('title', 'category', 'start_time', 'status')
    list_filter = ('status', 'category')
    search_fields = ('title', 'description', 'address')
    # gis_widget_kwargs налаштовує початковий вигляд карти (можна налаштувати на Львів)
    default_lon = 24.0297  # Довгота Львова
    default_lat = 49.8397  # Широта Львова
    default_zoom = 12