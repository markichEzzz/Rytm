from collections import Counter
from django.conf import settings
from django.db import migrations

def backfill(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    Profile = apps.get_model('accounts', 'UserProfile')
    users = list(User.objects.using(schema_editor.connection.alias).all().order_by('pk'))
    counts = Counter(user.email.strip().casefold() for user in users if user.email.strip())
    for user in users:
        email = user.email.strip().casefold()
        Profile.objects.using(schema_editor.connection.alias).get_or_create(user_id=user.pk, defaults={
            'display_name': (user.first_name + ' ' + user.last_name).strip()[:100],
            'email_key': email if email and counts[email] == 1 else None})

class Migration(migrations.Migration):
    dependencies = [('accounts', '0001_initial')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
