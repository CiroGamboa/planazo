"""Pydantic v2 row models for the identity aggregate — `users` + `preferences`.

Both fields match their columns in `planazo/storage/schema_v1.sql` 1:1, so a
row is validated on the way in (AGENTS.md rule 1) and reconstructed on the
way out. `id`/`created_at`/`updated_at` are `None` until the row exists.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


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
