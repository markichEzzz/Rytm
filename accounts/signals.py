from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import UserProfile

@receiver(post_save, sender=get_user_model())
def ensure_profile(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    email = instance.email.strip().casefold() or None
    defaults = {'display_name': instance.get_full_name()[:100], 'email_key': email}
    try:
        with transaction.atomic():
            UserProfile.objects.get_or_create(user=instance, defaults=defaults)
    except IntegrityError:
        # Preserve legacy/admin-created same-email users rather than merging them.
        UserProfile.objects.get_or_create(user=instance, defaults={**defaults, 'email_key': None})
