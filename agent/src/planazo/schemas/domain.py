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
from typing import Literal

from pydantic import BaseModel, Field

ApprovalOutcome = Literal["approve", "reject"]


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
    """One `preferences` row — a flat key/value filter preference for one user."""

    user_id: int = Field(ge=1)
    key: str = Field(min_length=1)
    value: str
    updated_at: datetime | None = None


class ApprovalDecision(BaseModel):
    """One `approvals` row — the audit trail for an approval-gate decision."""

    id: int | None = None
    user_id: int = Field(ge=1)
    artifact_kind: str = Field(min_length=1)
    artifact_id: int
    decision: ApprovalOutcome
    decided_at: datetime | None = None


class ExtractionRunIndexEntry(BaseModel):
    """One `extraction_runs_index` row — a pointer into the Extractor's run log."""

    id: int | None = None
    run_id: str = Field(min_length=1)
    user_id: int = Field(ge=1)
    url: str = Field(min_length=1)
    started_at: datetime | None = None
