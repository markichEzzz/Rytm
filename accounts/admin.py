from django.contrib import admin
from .models import UserProfile, UserIdentity

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'display_name', 'created_at']
    readonly_fields = ['user', 'email_key', 'created_at', 'updated_at']
    search_fields = ['display_name', 'user__username', 'user__email']

@admin.register(UserIdentity)
class UserIdentityAdmin(admin.ModelAdmin):
    list_display = ['user', 'provider', 'created_at']
    readonly_fields = ['user', 'provider', 'provider_subject', 'email', 'created_at']
    def has_add_permission(self, request):
        return False
