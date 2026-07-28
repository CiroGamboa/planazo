"""Pydantic v2 row models for the identity aggregate — `users` + `preferences`.

Both fields match their columns in `planazo/storage/schema_v1.sql` 1:1, so a
row is validated on the way in (AGENTS.md rule 1) and reconstructed on the
way out. `id`/`created_at`/`updated_at` are `None` until the row exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


class UserRecord(BaseModel):
    """One `users` row — the multi-user seam, keyed externally by Telegram id."""

    id: int | None = None
    telegram_user_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    created_at: datetime | None = None


class PreferenceRecord(BaseModel):
    """One `preferences` row — a flat key/value filter preference for one user.

    `key` and `value` are rendered together onto one line of an agent run's
    system message (`planazo.agents.event_agent` assembles the push context),
    so both are bounded on the two axes that matter there: length, and staying
    on the single line they are rendered onto. Outer whitespace is trimmed and
    a line break is rejected, not
    stripped — a field that opens a second line can read as a fresh instruction
    line in the pushed text, and silently rewriting it to fit would be the
    coerced success rule 4 forbids. 64 characters holds a filter name like
    `preferred_hours` several times over, and 200 holds a filter phrase like
    `techno, jazz, indie` several times over, which keeps any one row far below
    the push budget `data/rules/` is held to.
    """

    user_id: int = Field(ge=1)
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=200)
    updated_at: datetime | None = None

    @field_validator("key", "value", mode="before")
    @classmethod
    def _normalizes_and_stays_on_one_line(cls, value: object, info: ValidationInfo) -> str:
        # `splitlines` is the widest available definition of a line break — it
        # covers U+2028/U+2029 and the C1 separators as well as \n and \r, all
        # of which start a new line in the rendered system message.
        if not isinstance(value, str):
            raise ValueError(f"preference {info.field_name} must be a string")
        trimmed = value.strip()
        if trimmed and trimmed.splitlines() != [trimmed]:
            raise ValueError(f"preference {info.field_name} must be a single line: {value!r}")
        return trimmed


class PreferenceReadResult(BaseModel):
    """Validated preference rows, or a safe failure to reconstruct them.

    Repository reads fail closed: one malformed persisted row suppresses every
    row rather than letting a partial set enter model-visible push context.
    """

    rows: tuple[PreferenceRecord, ...] = ()
    error_type: Literal["invalid_preference_data", "preference_store_unavailable"] | None = None
    message: str = ""

    @model_validator(mode="after")
    def _has_one_outcome(self) -> PreferenceReadResult:
        if self.error_type is None:
            return self
        if self.rows:
            raise ValueError("preference read failure cannot include rows")
        if not self.message:
            raise ValueError("preference read failure needs a safe message")
        return self
