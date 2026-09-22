from django.contrib.gis.db import models

class Organizer(models.Model):
    name = models.CharField(max_length=255, verbose_name="Назва організатора")
    contact_email = models.EmailField(verbose_name="Контакт для зв'язку")
    verified = models.BooleanField(default=False, verbose_name="Підтверджений")

    def __str__(self):
        return self.name

class Event(models.Model):
    CATEGORY_CHOICES = [
        ('restaurants', 'Ресторани'),
        ('museums', 'Музеї'),
        ('nature', 'Природа'),
        ('party', 'Тусовки'),
        ('routes', 'Маршрути'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Очікує модерації'),
        ('approved', 'Затверджено'),
        ('rejected', 'Відхилено'),
    ]

    title = models.CharField(max_length=255, verbose_name="Назва події")
    description = models.TextField(verbose_name="Опис")
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, verbose_name="Категорія")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="Ціна (пусто=безкоштовно)")
    start_time = models.DateTimeField(verbose_name="Початок події")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="Кінець події")
    
    # Географічна точка PostGIS
    location = models.PointField(verbose_name="Координати")
    address = models.CharField(max_length=255, verbose_name="Адреса")
    
    photo = models.ImageField(upload_to='events_photos/', verbose_name="Фото")
    source_url = models.URLField(null=True, blank=True, verbose_name="Посилання на джерело")
    
    organizer = models.ForeignKey(Organizer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Організатор")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="Статус")

    def __str__(self):
        return self.title