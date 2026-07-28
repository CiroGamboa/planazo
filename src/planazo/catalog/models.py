"""Pydantic v2 row models for the catalog aggregate — `events` + `extraction_runs_index`.

Every field matches its column in `planazo/storage/migrations/` 1:1, so a
row is validated on the way in (AGENTS.md rule 1 — a `ValidationError` at
the repository boundary becomes an `invalid_event_data` typed error, never
a partial row) and reconstructed on the way out.

`id` and the `*_at` timestamps are `None` until the row exists: a caller
builds an `Event` to insert without knowing its id, and the repository
stamps `ingested_at` / `started_at` when the row is written.

`category` is the `EventCategory` Literal owned by `query/models.py`. The
same Literal constrains both the interpreter's `SearchIntent.categories`
tuple and this row: a value outside the set trips `ValidationError` at
model construction, which the repository/tool layer turns into an
`invalid_event_data` typed error rather than a silently coerced string.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from planazo.query.models import EventCategory


class Event(BaseModel):
    """One `events` row — the shared domain surface both agents read and write."""

    id: int | None = None
    source: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_utc: datetime
    end_utc: datetime
    category: EventCategory
    city: str = Field(min_length=1)
    price_cents: int = Field(default=0, ge=0)
    geo_lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    geo_lng: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0)
    # `extra` absorbs source-specific fields without a schema change; it is
    # stored as a JSON-encoded object in the `extra` TEXT column.
    extra: dict[str, object] = Field(default_factory=dict)
    ingested_at: datetime | None = None
    # A post announcing multiple distinct events becomes N rows, one per event,
    # indexed by `event_index_in_post` starting at 0. The natural key is the
    # composite `(source_url, event_index_in_post)`.
    event_index_in_post: int = Field(default=0, ge=0)
    # Domain-model columns added by migration 002 (issue #88). Every string
    # field is nullable because not every source exposes every field; `tags` is
    # a JSON array stored as TEXT under the same discipline as `extra`;
    # `recurring` is 0/1 in SQLite and a bool here.
    source_account: str | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    organizer: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    ticket_url: str | None = None
    image_url: str | None = None
    language: str | None = None
    recurring: bool = False

    @model_validator(mode="after")
    def _coordinates_are_a_complete_pair(self) -> Event:
        if (self.geo_lat is None) != (self.geo_lng is None):
            raise ValueError("geo_lat and geo_lng must both be present or both be absent")
        return self


class ExtractionRunIndexEntry(BaseModel):
    """One `extraction_runs_index` row — a pointer into the Extractor's run log."""

    id: int | None = None
    run_id: str = Field(min_length=1)
    user_id: int = Field(ge=1)
    url: str = Field(min_length=1)
    started_at: datetime | None = None
