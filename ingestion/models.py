from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from events.models import Event
from events.pricing import PriceSemanticsMixin, PriceStatus


class SourceAccount(models.Model):
    class Platform(models.TextChoices):
        INSTAGRAM = 'instagram', 'Instagram'
        TELEGRAM = 'telegram', 'Telegram'

    platform = models.CharField(max_length=30, choices=Platform.choices, default=Platform.INSTAGRAM)
    username = models.CharField(max_length=150, validators=[
        RegexValidator(r'^[A-Za-z0-9_.]+$', 'Use a username without @, spaces, or URL.'),
    ])
    display_name = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    source_url = models.URLField(max_length=2048, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['platform', 'username']
        constraints = [models.UniqueConstraint(Lower('username'), 'platform', name='unique_source_platform_username')]

    def save(self, *args, **kwargs):
        self.username = self.username.strip().lower().lstrip('@')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.platform}: @{self.username}'


class RawSourceItem(models.Model):
    class ExtractionStatus(models.TextChoices):
        NEW = 'new', 'New'
        EXTRACTED = 'extracted', 'Extracted'
        IGNORED = 'ignored', 'Ignored'
        FAILED = 'failed', 'Failed'

    source_account = models.ForeignKey(SourceAccount, on_delete=models.PROTECT, related_name='raw_items')
    external_id = models.CharField(max_length=255)
    source_url = models.URLField(max_length=2048)
    published_at = models.DateTimeField(null=True, blank=True)
    raw_text = models.TextField(blank=True)
    raw_image_url = models.URLField(max_length=2048, blank=True)
    raw_metadata = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)
    extraction_status = models.CharField(max_length=20, choices=ExtractionStatus.choices, default=ExtractionStatus.NEW)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-fetched_at']
        constraints = [models.UniqueConstraint(fields=['source_account', 'external_id'], name='unique_raw_source_external_id')]

    def __str__(self):
        return f'{self.source_account} / {self.external_id}'


class EventCandidate(PriceSemanticsMixin, models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Needs review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        DUPLICATE = 'duplicate', 'Likely duplicate'

    raw_source_item = models.ForeignKey(RawSourceItem, on_delete=models.PROTECT, related_name='candidates', null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='submitted_candidates', editable=False)
    source_fingerprint = models.CharField(max_length=64, blank=True, default='', editable=False)
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True, help_text='Internal extraction draft. Public description is generated from facts on approval.')
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    venue_name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=255, blank=True)
    latitude = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)])
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    price_status = models.CharField(max_length=10, choices=PriceStatus.choices, default=PriceStatus.UNKNOWN)
    category = models.CharField(max_length=50, choices=Event.CATEGORY_CHOICES, blank=True)
    source_url = models.URLField(max_length=2048, blank=True)
    source_image_url = models.URLField(max_length=2048, blank=True, help_text='Internal reference only; never used as public Event photo.')
    public_photo = models.ImageField(upload_to='candidate_photos/', blank=True,
        help_text='Manually upload a photo you own or have permission to publish. Required before approval.')
    use_source_image_as_public_photo = models.BooleanField(default=False,
        verbose_name='Use source image as public event photo',
        help_text='Explicitly authorize copying the source image to public storage during approval. A manual upload takes priority.')
    source_photo_authorized_at = models.DateTimeField(null=True, blank=True, editable=False)
    source_photo_authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='authorized_source_photos', editable=False)
    confidence = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(1)])
    extraction_notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, editable=False)
    event = models.OneToOneField(Event, on_delete=models.SET_NULL, null=True, blank=True, related_name='ingestion_candidate', editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reviewed_candidates', editable=False)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['raw_source_item', 'source_fingerprint'],
            condition=~models.Q(source_fingerprint=''), name='unique_raw_candidate_fingerprint')]

    @property
    def source_account(self):
        return self.raw_source_item.source_account if self.raw_source_item_id else None

    def __str__(self):
        return self.title or f'Candidate #{self.pk or "new"} from {self.source_account}'


class LocationLookup(models.Model):
    """Private persistent cache and application-wide Nominatim request clock."""
    key = models.CharField(max_length=64, primary_key=True)
    query = models.CharField(max_length=600)
    result = models.JSONField(default=dict)
    looked_up_at = models.DateTimeField(default=timezone.now, db_index=True)


class EventSourceObservation(models.Model):
    """Private per-event source facts linked to one canonical candidate or public event."""
    raw_source_item = models.ForeignKey(RawSourceItem, on_delete=models.PROTECT, related_name='observations')
    fingerprint = models.CharField(max_length=64)
    candidate = models.ForeignKey(EventCandidate, on_delete=models.SET_NULL, null=True, blank=True, related_name='observations')
    event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True, related_name='source_observations')
    match_status = models.CharField(max_length=20, choices=[('unique','Unique'),('confirmed','Confirmed match'),('probable','Probable duplicate')], default='unique')
    matched_candidate = models.ForeignKey(EventCandidate, on_delete=models.SET_NULL, null=True, blank=True, related_name='probable_observations')
    matched_event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True, related_name='probable_source_observations')
    facts = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['raw_source_item', 'fingerprint'], name='unique_source_event_observation')]
