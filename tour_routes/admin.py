from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from .models import TourRoute, TourRouteStop

class RouteStopFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors): return
        stops=[form.instance for form in self.forms if form.cleaned_data and not form.cleaned_data.get('DELETE')]
        orders=[stop.order for stop in stops]
        if len(set(orders)) != len(orders): raise ValidationError('Порядок зупинок має бути унікальним.')
        if self.instance.is_published: self.instance.validate_for_publication(stops)

class RouteStopInline(admin.StackedInline):
    model=TourRouteStop
    formset=RouteStopFormSet
    extra=1
    ordering=['order']

@admin.register(TourRoute)
class TourRouteAdmin(admin.ModelAdmin):
    list_display=['title','route_type','estimated_duration_minutes','difficulty','is_published','submitted_by']
    list_filter=['is_published','route_type','difficulty']
    search_fields=['title','description','stops__title']
    inlines=[RouteStopInline]
    readonly_fields=['created_at','updated_at','submitted_by']

    def save_model(self, request, obj, form, change):
        # Inline formset validates the proposed stop list before anything is saved.
        obj._publish_after_inlines=obj.is_published
        obj.is_published=False
        super().save_model(request,obj,form,change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request,form,formsets,change)
        obj=form.instance
        obj.is_published=obj._publish_after_inlines
        obj.save()
