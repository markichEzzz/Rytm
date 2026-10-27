from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('accounts', '0002_existing_profiles')]
    operations = [migrations.CreateModel(name='DeletedAccountMedia', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('name', models.CharField(max_length=1024, unique=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
    ])]
