"""Price semantics shared by public events and private candidates."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models


class PriceStatus(models.TextChoices):
    PAID = 'paid', 'Paid'
    FREE = 'free', 'Free'
    UNKNOWN = 'unknown', 'Unknown'


def infer_price_status(price):
    return PriceStatus.UNKNOWN if price is None else PriceStatus.FREE if Decimal(price) == 0 else PriceStatus.PAID


def normalize_price(status, price):
    if status == PriceStatus.UNKNOWN:
        return None
    if status == PriceStatus.FREE:
        return Decimal('0.00')
    if status != PriceStatus.PAID or price is None:
        raise ValidationError({'price': 'A numeric price is required for Paid.'})
    if not Decimal(price).is_finite() or Decimal(price) < 0:
        raise ValidationError({'price': 'Paid requires a valid non-negative numeric price.'})
    return price


class PriceSemanticsMixin:
    def __init__(self, *args, **kwargs):
        infer = not args and 'price_status' not in kwargs
        super().__init__(*args, **kwargs)
        if infer:
            self.price_status = infer_price_status(self.price)

    def clean(self):
        super().clean()
        self.price = normalize_price(self.price_status, self.price)

    def save(self, *args, **kwargs):
        # Django save permits incomplete drafts; paid amounts are validated by
        # clean()/the API and approval, just like the existing numeric field.
        if self.price_status != PriceStatus.PAID:
            self.price = normalize_price(self.price_status, self.price)
        fields = kwargs.get('update_fields')
        if fields and {'price', 'price_status'} & set(fields):
            kwargs['update_fields'] = set(fields) | {'price', 'price_status'}
        return super().save(*args, **kwargs)
