"""Private moderator edit protection shared by extraction and enrichment."""
import json

from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.contenttypes.models import ContentType


def moderator_edited_fields(candidate):
    protected = set()
    for line in candidate.extraction_notes.splitlines():
        if line.startswith(('[Location facts] Moderator edited: ', '[Pricing] Moderator edited: ', '[Candidate facts] Moderator edited: ')):
            protected.update(field.strip() for field in line.split(': ', 1)[1].split(','))
    if not candidate.pk:
        return protected
    content_type = ContentType.objects.filter(app_label=candidate._meta.app_label, model=candidate._meta.model_name).first()
    labels = {str(field.verbose_name).casefold(): field.name for field in candidate._meta.fields}
    labels.update({field.name: field.name for field in candidate._meta.fields})
    for message in LogEntry.objects.filter(content_type=content_type, object_id=str(candidate.pk), action_flag=CHANGE).values_list('change_message', flat=True):
        try:
            for entry in json.loads(message):
                for label in entry.get('changed', {}).get('fields', []):
                    if label.casefold() in labels:
                        protected.add(labels[label.casefold()])
        except (ValueError, TypeError, AttributeError):
            continue
    return protected


def address_source(candidate):
    if 'address' in moderator_edited_fields(candidate):
        return 'moderator'
    for line in reversed(candidate.extraction_notes.splitlines()):
        if line.startswith('[Address] '):
            return line.removeprefix('[Address] ').split(':', 1)[0]
    if 'address_provenance:' in candidate.extraction_notes or 'address: caption' in candidate.extraction_notes or 'address: ocr' in candidate.extraction_notes or 'address=event_specific' in candidate.extraction_notes or 'address=shared_post_context' in candidate.extraction_notes:
        return 'extracted'
    return ''
