from django.db import migrations

def forwards(apps, schema_editor):
    Candidate = apps.get_model('ingestion', 'EventCandidate')
    for candidate in Candidate.objects.using(schema_editor.connection.alias).filter(submitted_by__isnull=True, event__submitted_by__isnull=False).select_related('event').iterator():
        candidate.submitted_by_id = candidate.event.submitted_by_id
        candidate.save(update_fields=['submitted_by'])

class Migration(migrations.Migration):
    dependencies = [('ingestion', '0007_eventcandidate_submitted_by_and_more')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
