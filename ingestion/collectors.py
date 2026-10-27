"""Normalized collector boundary shared by external source adapters."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .models import SourceAccount


@dataclass(frozen=True)
class CollectedSourceItem:
    external_id: str
    source_url: str
    raw_text: str = ''
    published_at: datetime | None = None
    raw_image_url: str = ''
    raw_metadata: dict = field(default_factory=dict)


class SourceCollector(Protocol):
    def fetch_items(self, source_account: SourceAccount) -> list[CollectedSourceItem]: ...
