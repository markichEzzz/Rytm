from django.contrib.gis.db import models
from django.conf import settings
from django.utils import timezone
from .pricing import PriceSemanticsMixin, PriceStatus

class Organizer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='organizer_profile', verbose_name="Акаунт організатора",
    )
    name = models.CharField(max_length=255, verbose_name="Назва організатора")
    contact_email = models.EmailField(verbose_name="Контакт для зв'язку")
    verified = models.BooleanField(default=False, verbose_name="Підтверджений")

    def __str__(self):
        return self.name

class Event(PriceSemanticsMixin, models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='submitted_events', editable=False,
    )
    CATEGORY_CHOICES = [
        ('restaurants', 'Ресторани'),
        ('museums', 'Музеї'),
        ('nature', 'Природа'),
        ('party', 'Тусовки'),
        ('routes', 'Маршрути'),
        ('education', 'Навчання / Майстер-класи'),
        ('performance', 'Вистави / Кіно'),
        ('concert', 'Концерти'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Очікує модерації'),
        ('approved', 'Затверджено'),
        ('rejected', 'Відхилено'),
    ]

    title = models.CharField(max_length=255, verbose_name="Назва події")
    description = models.TextField(verbose_name="Опис")
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, verbose_name="Категорія")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="Ціна")
    price_status = models.CharField(max_length=10, choices=PriceStatus.choices, default=PriceStatus.UNKNOWN)
    start_time = models.DateTimeField(verbose_name="Початок події")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="Кінець події")
    
    # Географічна точка PostGIS
    location = models.PointField(verbose_name="Координати")
    address = models.CharField(max_length=255, verbose_name="Адреса")
    
    photo = models.ImageField(upload_to='events_photos/', verbose_name="Фото")
    source_url = models.URLField(null=True, blank=True, verbose_name="Посилання на джерело")
    
    organizer = models.ForeignKey(Organizer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Організатор")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="Статус")

    class Meta:
        indexes = [models.Index(fields=['submitted_by', '-created_at'], name='event_submitter_created_idx'),
                   models.Index(fields=['status','start_time'], name='event_public_start_idx')]

    def __str__(self):
        return self.title


class EventReport(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Очікує розгляду'
        UNDER_REVIEW = 'under_review', 'На розгляді'
        CONFIRMED = 'confirmed', 'Підтверджено'
        DISMISSED = 'dismissed', 'Відхилено'

    class Reason(models.TextChoices):
        INCORRECT_INFORMATION = 'incorrect_information', 'Неточна інформація'
        EVENT_DOES_NOT_EXIST = 'event_does_not_exist', 'Події не існує'
        WRONG_DATE_TIME = 'wrong_date_time', 'Неправильна дата або час'
        WRONG_LOCATION = 'wrong_location', 'Неправильне місце'
        SPAM_OR_SCAM = 'spam_or_scam', 'Спам або шахрайство'
        INAPPROPRIATE_CONTENT = 'inappropriate_content', 'Неприйнятний вміст'
        OTHER = 'other', 'Інше'

    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                 related_name='event_reports')
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='reports')
    reason = models.CharField(max_length=32, choices=Reason.choices)
    description = models.TextField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='reviewed_event_reports', editable=False)
    moderator_note = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['reporter', 'event'],
            condition=~models.Q(status='dismissed'), name='unique_non_dismissed_event_report')]
        indexes = [models.Index(fields=['status', '-created_at'], name='report_status_created_idx')]

    def __str__(self):
        return f'#{self.pk}: {self.event} — {self.get_reason_display()}'


class UserEventInteraction(models.Model):
    class Type(models.TextChoices):
        VIEW = 'view', 'Перегляд'
        INTERESTED = 'interested', 'Цікавить'
        NOT_INTERESTED = 'not_interested', 'Не цікаво'
        MAP_OPEN = 'map_open', 'Відкриття карти'
        ORGANIZER_OPEN = 'organizer_open', 'Відкриття організатора'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_interactions')
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='user_interactions')
    interaction_type = models.CharField(max_length=20, choices=Type.choices)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_interacted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'event', 'interaction_type'], name='unique_user_event_signal'),
            models.UniqueConstraint(fields=['user', 'event'], condition=models.Q(interaction_type__in=['interested','not_interested']), name='unique_user_event_preference')]
        indexes = [models.Index(fields=['user', '-last_interacted_at'], name='interaction_user_recent_idx'),
                   models.Index(fields=['event', 'interaction_type', '-last_interacted_at'], name='interaction_event_type_idx')]
