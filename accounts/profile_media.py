"""Owned profile updates with private storage keys and durable cleanup intents."""
from datetime import timedelta
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, AuthenticationFailed
from .models import UserProfile, DeletedAccountMedia
from .deletion import purge_deleted_media


class ProfileMediaUnavailable(APIException):
    status_code = 503
    default_detail = 'Не вдалося зберегти фото. Спробуйте ще раз.'


def update_profile(instance, data):
    data = dict(data)
    upload = data.pop('avatar', None)
    remove = data.pop('remove_avatar', False)
    new_name = None
    intent_names = []
    try:
        if upload is not None:
            intended = f'profile_avatars/{uuid4().hex}'
            if hasattr(default_storage, 'prepare_profile_avatar_name'):
                intended = default_storage.prepare_profile_avatar_name(intended)
            # Persist before cloud I/O: a failed request/process can be retried
            # by cleanup_account_media without retaining user identity or bytes.
            ready = timezone.now() + timedelta(hours=1)
            DeletedAccountMedia.objects.get_or_create(name=intended, defaults={'available_at': ready})
            intent_names.append(intended)
            new_name = default_storage.save(intended, upload)
            if new_name != intended:
                DeletedAccountMedia.objects.get_or_create(name=new_name, defaults={'available_at': ready})
                intent_names.append(new_name)
        with transaction.atomic():
            # Same lock order as account deletion; never restore a deleted user.
            if not get_user_model().objects.select_for_update().filter(pk=instance.user_id, is_active=True).exists():
                raise AuthenticationFailed('Account is unavailable.')
            profile = UserProfile.objects.select_for_update().get(pk=instance.pk, user_id=instance.user_id)
            old_name = profile.avatar.name if profile.avatar else ''
            for key, value in data.items():
                setattr(profile, key, value)
            if new_name is not None or remove:
                profile.avatar = new_name or ''
                profile.avatar_url = ''  # Do not revive an old Google photo after removal.
            profile.save()
            if old_name and (new_name is not None or remove) and old_name != new_name:
                DeletedAccountMedia.objects.get_or_create(name=old_name)
            DeletedAccountMedia.objects.filter(name__in=intent_names).delete()
            transaction.on_commit(purge_deleted_media, robust=True)
        return profile
    except Exception as error:
        if intent_names:
            DeletedAccountMedia.objects.filter(name__in=intent_names).update(available_at=timezone.now())
            transaction.on_commit(purge_deleted_media, robust=True)
        if isinstance(error, AuthenticationFailed):
            raise
        if upload is not None:
            raise ProfileMediaUnavailable() from None
        raise
