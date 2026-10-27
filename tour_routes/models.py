from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator

class RouteType(models.TextChoices):
    CULTURE = 'culture', 'Культурний'
    HISTORY = 'history', 'Історичний'
    ARCHITECTURE = 'architecture', 'Архітектурний'
    NATURE = 'nature', 'Природа'
    GASTRONOMY = 'gastronomy', 'Гастрономічний'
    ROMANTIC = 'romantic', 'Романтичний'
    HIDDEN_GEMS = 'hidden_gems', 'Незвичайний Львів'
    FAMILY = 'family', 'Для сім’ї'
    MIXED = 'mixed', 'Змішаний'

class Difficulty(models.TextChoices):
    EASY = 'easy', 'Легкий'
    MEDIUM = 'medium', 'Середній'
    HARD = 'hard', 'Тривалий / складний'

class TourRoute(models.Model):
    title = models.CharField(max_length=255, blank=True)
    short_description = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    route_type = models.CharField(max_length=20, choices=RouteType.choices, default=RouteType.MIXED)
    estimated_duration_minutes = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, default=Difficulty.EASY)
    cover_image = models.ImageField(upload_to='route_covers/', blank=True)
    is_published = models.BooleanField(default=False)
    demo_key = models.CharField(max_length=80, unique=True, null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title', 'id']
        indexes = [models.Index(fields=['is_published', 'route_type'], name='tour_public_type_idx')]

    def __str__(self): return self.title or 'Новий маршрут'

    def clean(self):
        if self.is_published and (not self.title.strip() or not self.estimated_duration_minutes or self.route_type not in RouteType.values):
            raise ValidationError('Опублікований маршрут потребує назви, типу та додатної тривалості.')

    def validate_for_publication(self, stops=None):
        self.clean()
        stops = list(stops if stops is not None else self.stops.all()) if self.pk else list(stops or [])
        if len(stops) < 2:
            raise ValidationError('Для публікації потрібно щонайменше дві зупинки.')
        for stop in stops:
            stop.clean()

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.is_published: self.validate_for_publication()
        super().save(*args, **kwargs)

    @property
    def distance_meters(self):
        if not hasattr(self, '_distance_meters'):
            from .distance import attach_distances
            attach_distances([self])
        return self._distance_meters

class TourRouteStop(models.Model):
    route = models.ForeignKey(TourRoute, on_delete=models.CASCADE, related_name='stops')
    order = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    latitude = models.FloatField(validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField(validators=[MinValueValidator(-180), MaxValueValidator(180)])
    address = models.CharField(max_length=255, blank=True)
    image_url = models.URLField(blank=True, max_length=2048)
    recommended_stop_minutes = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        constraints = [models.UniqueConstraint(fields=['route', 'order'], name='tour_unique_stop_order', deferrable=models.Deferrable.DEFERRED),
            models.CheckConstraint(condition=models.Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180), name='tour_valid_coordinates'),
            models.CheckConstraint(condition=models.Q(order__gte=1), name='tour_positive_stop_order')]

    def __str__(self): return f'{self.order}. {self.title}'

    def clean(self):
        import math
        if (not self.title.strip() or self.latitude is None or self.longitude is None
            or not math.isfinite(self.latitude) or not math.isfinite(self.longitude)
            or not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180):
            raise ValidationError('Зупинка потребує назви та дійсних координат.')

    def validate_constraints(self, exclude=None):
        # A reordered inline can temporarily occupy another row's old position.
        # The formset checks proposed positions; the deferred DB constraint checks the final transaction.
        for model_class, constraints in self.get_constraints():
            for constraint in constraints:
                if constraint.name != 'tour_unique_stop_order':
                    constraint.validate(model_class, self, exclude=exclude)

    def save(self, *args, **kwargs):
        self.full_clean(validate_constraints=False)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.route.is_published and self.route.stops.count() <= 2:
            raise ValidationError('Спочатку зніміть маршрут із публікації.')
        return super().delete(*args, **kwargs)
