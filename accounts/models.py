from django.conf import settings
from django.db import models

class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    display_name = models.CharField(max_length=100, blank=True)
    avatar_url = models.URLField(max_length=2048, blank=True)
    bio = models.CharField(max_length=1000, blank=True)
    # Private normalized reservation: built-in User.email is not unique. Legacy
    # duplicate/blank emails can retain their accounts without unsafe merging.
    email_key = models.EmailField(max_length=254, unique=True, null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class UserIdentity(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='identities')
    provider = models.CharField(max_length=20, choices=[('google', 'Google')])
    provider_subject = models.CharField(max_length=255)
    email = models.EmailField(max_length=254)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['provider', 'provider_subject'], name='unique_auth_identity'),
                       models.UniqueConstraint(fields=['user', 'provider'], name='unique_user_auth_provider')]


class DeletedAccountMedia(models.Model):
    """Durable storage cleanup outbox; no account identifiers or file contents."""
    name = models.CharField(max_length=1024, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
