from django.db import transaction
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
import firebase_admin
from firebase_admin import messaging
from .models import Event


@receiver(pre_save, sender=Event)
def remember_approval(sender, instance, **kwargs):
    instance._previously_approved = bool(instance.pk and Event.objects.filter(
        pk=instance.pk, status='approved',
    ).exists())


def _send_approval_notification(title, category):
    try:
        firebase_admin.get_app()
    except ValueError:
        return  # Firebase is optional locally.
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title='Нова подія: ' + title,
                body=f'Категорія: {category}. Знайди на карті!',
            ),
            topic='lviv_events',
        ))
    except Exception:
        # A notification failure must not invalidate an already committed event.
        import logging
        logging.getLogger(__name__).warning('Approval notification could not be sent.')


@receiver(post_save, sender=Event)
def send_push_on_approval(sender, instance, raw=False, **kwargs):
    if (raw or instance.status != 'approved' or getattr(instance, '_previously_approved', False)
            or getattr(instance, '_skip_approval_notification', False)):
        return
    title, category = instance.title, instance.get_category_display()
    transaction.on_commit(lambda: _send_approval_notification(title, category))

@receiver(post_save, sender=Event)
def sync_user_candidate_status(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from ingestion.models import EventCandidate
    mapped = {'pending': 'new', 'approved': 'approved', 'rejected': 'rejected'}[instance.status]
    # User submission Event and its candidate are one logical submission. Machine
    # extraction provenance and moderation rules remain on their existing path.
    EventCandidate.objects.filter(event=instance, raw_source_item__isnull=True).exclude(status=mapped).update(status=mapped)
