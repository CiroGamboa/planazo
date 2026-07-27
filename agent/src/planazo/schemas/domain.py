"""Pydantic v2 row models for the SQLite domain store, one per table.

Every field matches its column in `planazo/storage/schema_v1.sql` 1:1, so a
row is validated on the way in (AGENTS.md rule 1 — a `ValidationError` at the
dao boundary becomes an `invalid_event_data` typed error, never a partial row)
and reconstructed on the way out.

`id` and the `*_at` timestamps are `None` until the row exists: a caller
builds an `Event` to insert without knowing its id, and the dao stamps
`ingested_at`/`created_at`/`decided_at`/`started_at`/`updated_at` when the row
is written.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class Event(BaseModel):
    """One `events` row — the shared domain surface both agents read and write."""

    id: int | None = None
    source: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_utc: datetime
    end_utc: datetime
    category: str = Field(min_length=1)
    city: str = Field(min_length=1)
    price_cents: int = Field(default=0, ge=0)
    geo_lat: float | None = None
    geo_lng: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    # `extra` absorbs source-specific fields without a schema change; it is
    # stored as a JSON-encoded object in the `extra` TEXT column.
    extra: dict[str, object] = Field(default_factory=dict)
    ingested_at: datetime | None = None


class UserRecord(BaseModel):
    """One `users` row — the multi-user seam, keyed externally by Telegram id."""

    id: int | None = None
    telegram_user_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    created_at: datetime | None = None


class PreferenceRecord(BaseModel):
    """One `preferences` row — a flat key/value filter preference for one user.

    `value` is the one row field rendered into an agent run's system message
    (`planazo.agents.event_agent` assembles the push context), so it is bounded
    on both axes that matter there. A line break is rejected, not stripped: a
    value that opens a second line can read as a fresh instruction line in the
    pushed text, and silently rewriting it to fit would be the coerced success
    rule 4 forbids. 200 characters holds a filter phrase like
    `techno, jazz, indie` several times over while keeping any one row far
    below the push budget `data/rules/` is held to.
    """

    user_id: int = Field(ge=1)
    key: str = Field(min_length=1)
    value: str = Field(max_length=200)
    updated_at: datetime | None = None

    @field_validator("value")
    @classmethod
    def _stays_on_one_line(cls, value: str) -> str:
        # `splitlines` is the widest available definition of a line break — it
        # covers U+2028/U+2029 and the C1 separators as well as \n and \r, all
        # of which start a new line in the rendered system message.
        if value and value.splitlines() != [value]:
            raise ValueError(f"preference value must be a single line: {value!r}")
        return value


class ExtractionRunIndexEntry(BaseModel):
    """One `extraction_runs_index` row — a pointer into the Extractor's run log."""

    id: int | None = None
    run_id: str = Field(min_length=1)
    user_id: int = Field(ge=1)
    url: str = Field(min_length=1)
    started_at: datetime | None = None
