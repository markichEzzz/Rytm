"""Own-account erasure. Public event facts remain; private drafts and links do not."""
import logging
from django.contrib.auth import get_user_model
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from events.models import Event, EventReport, Organizer
from ingestion.models import EventCandidate
from tour_routes.models import TourRoute, TourRouteStop
from .models import DeletedAccountMedia, UserProfile, UserIdentity


def purge_deleted_media():
    """Retryable; only our storage keys, never arbitrary URLs. Failures stay queued."""
    for pending in DeletedAccountMedia.objects.order_by('pk').iterator():
        # A shared public image must not disappear with its former submitter.
        if (Event.objects.filter(photo=pending.name).exists() or
                EventCandidate.objects.filter(public_photo=pending.name).exists() or
                TourRoute.objects.filter(cover_image=pending.name).exists()):
            pending.delete()
            continue
        try:
            default_storage.delete(pending.name)
        except Exception:
            logging.getLogger(__name__).warning('Account media cleanup pending; retry cleanup_account_media.')
        else:
            pending.delete()


@transaction.atomic
def delete_account(user):
    user = get_user_model().objects.select_for_update().filter(pk=user.pk).first()
    if user is None:
        return  # Concurrent deletion already completed.
    candidates = EventCandidate.objects.filter(submitted_by=user)
    private_events = Event.objects.filter(submitted_by=user).exclude(status='approved')
    routes = TourRoute.objects.filter(submitted_by=user)
    private_routes = routes.filter(is_published=False)
    images = set(candidates.exclude(public_photo='').values_list('public_photo', flat=True))
    images.update(private_events.exclude(photo='').values_list('photo', flat=True))
    images.update(private_routes.exclude(cover_image='').values_list('cover_image', flat=True))
    for name in images:
        if name:
            DeletedAccountMedia.objects.get_or_create(name=name)

    # Keep report reason/status/timestamps, remove the reporter's free text and
    # a departing reviewer's free-form notes (which may contain personal data).
    EventReport.objects.filter(reporter=user).update(reporter=None, description='', moderator_note='')
    EventReport.objects.filter(reviewed_by=user).update(reviewed_by=None, moderator_note='')
    # A legacy organizer contains identifying name/email outside its User FK.
    organizer_ids = list(Organizer.objects.filter(user=user).values_list("pk", flat=True))
    Organizer.objects.filter(user=user).delete()  # Events' organizer FK SET_NULL.
    # No private submission draft/rejection survives. Approved Event stays public.
    candidate_ids = list(candidates.values_list('pk', flat=True))
    event_ids = list(private_events.values_list('pk', flat=True))
    for model, ids in ((get_user_model(), [user.pk]), (UserProfile, list(UserProfile.objects.filter(user=user).values_list("pk", flat=True))),
                       (UserIdentity, list(user.identities.values_list('pk', flat=True))),
                       (Organizer, organizer_ids),
                       (OutstandingToken, list(OutstandingToken.objects.filter(user=user).values_list('pk', flat=True))),
                       (BlacklistedToken, list(BlacklistedToken.objects.filter(token__user=user).values_list('pk', flat=True))),
                       (EventCandidate, candidate_ids), (Event, event_ids),
                       (TourRoute, list(routes.values_list('pk', flat=True))),
                       (TourRouteStop, list(TourRouteStop.objects.filter(route__in=routes).values_list('pk', flat=True)))):
        ct = ContentType.objects.get_for_model(model)
        LogEntry.objects.filter(content_type=ct, object_id__in=[str(pk) for pk in ids]).delete()
    # Preserve edit protection as field names, never the departed moderator's
    # identity. Some legacy candidates only recorded edits in admin logs.
    from ingestion.provenance import moderator_edited_fields
    candidate_type = ContentType.objects.get_for_model(EventCandidate)
    edited_ids = LogEntry.objects.filter(user=user, content_type=candidate_type).values_list('object_id', flat=True)
    for candidate in EventCandidate.objects.filter(pk__in=[int(pk) for pk in edited_ids if pk.isdigit()]):
        if candidate.pk in candidate_ids:
            continue
        fields = moderator_edited_fields(candidate)
        if fields:
            candidate.extraction_notes += '\n[Candidate facts] Moderator edited: ' + ','.join(sorted(fields))
            candidate.save(update_fields=['extraction_notes'])
    candidates.delete()
    private_events.delete()
    private_routes.delete()
    # Match event erasure: remove private route drafts, detach public facts and
    # clear submitter-authored descriptions that can contain account identity.
    TourRouteStop.objects.filter(route__in=routes).update(description='')
    routes.update(submitted_by=None, description='', short_description='')
    # Public facts/photos are content, not a retained account profile. Clear
    # submitter-authored free text and source links which can carry identity.
    Event.objects.filter(submitted_by=user).update(submitted_by=None, description='', source_url=None)
    # Remove stored bearer credentials too. Refresh endpoint explicitly rejects
    # tokens for missing users, including previously issued untracked tokens.
    OutstandingToken.objects.filter(user=user).delete()
    for session in Session.objects.all().iterator():
        if str(session.get_decoded().get('_auth_user_id')) == str(user.pk):
            session.delete()
    user.delete()  # profiles, identities, interactions, own admin logs CASCADE;
                   # other candidates/reports' reviewer/authorization FK SET_NULL.
    transaction.on_commit(purge_deleted_media, robust=True)
