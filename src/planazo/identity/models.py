"""Pydantic v2 row models for the identity aggregate — `users` + `preferences`.

`UserRecord`'s fields match their columns in `planazo/storage/schema_v1.sql`
and `schema_v2.sql` 1:1, so a row is validated on the way in (AGENTS.md rule
1) and reconstructed on the way out. `id` and `created_at` are `None` until
the row exists; the five registration fields (`age`, `location`, `language`,
`nationality`, `pending_registration_field`) are `None` until the guided
registration flow writes them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

ProfileField = Literal["display_name", "age", "location", "language", "nationality"]
"""The `users` columns the guided registration flow can point at or write.

Used by `UserRecord`'s own fields and by the repository functions
(`set_pending_registration_field`, `record_registration_answer`) that move
the `pending_registration_field` pointer or write an answer.
"""


class UserRecord(BaseModel):
    """One `users` row — the multi-user seam, keyed externally by Telegram id."""

    id: int | None = None
    telegram_user_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    created_at: datetime | None = None
    age: int | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, min_length=1, max_length=500)
    language: str | None = Field(default=None, min_length=1, max_length=500)
    nationality: str | None = Field(default=None, min_length=1, max_length=500)
    pending_registration_field: ProfileField | None = None

    @property
    def profile_complete(self) -> bool:
        """`True` iff `age`, `location`, `language`, and `nationality` are all set.

        `display_name` is excluded deliberately (see ADR 0013): create-on-
        first-contact always populates it before registration ever runs, so
        it cannot distinguish a registered user from an unregistered one the
        way the other four fields can.
        """
        return (
            self.age is not None
            and self.location is not None
            and self.language is not None
            and self.nationality is not None
        )

    @property
    def is_mid_registration(self) -> bool:
        """`True` iff a registration step is waiting on this user's next answer."""
        return self.pending_registration_field is not None


class PreferenceRecord(BaseModel):
    """One `preferences` row — a flat key/value filter preference for one user.

    `key` and `value` are rendered together onto one line of an agent run's
    system message (`planazo.agents.event_agent` assembles the push context),
    so both are bounded on the two axes that matter there: length, and staying
    on the single line they are rendered onto. A line break is rejected, not
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

    @field_validator("key", "value")
    @classmethod
    def _stays_on_one_line(cls, value: str, info: ValidationInfo) -> str:
        # `splitlines` is the widest available definition of a line break — it
        # covers U+2028/U+2029 and the C1 separators as well as \n and \r, all
        # of which start a new line in the rendered system message.
        if value and value.splitlines() != [value]:
            raise ValueError(f"preference {info.field_name} must be a single line: {value!r}")
        return value


class PreferenceReadResult(BaseModel):
    """Validated preference rows, or a safe failure to reconstruct them.

    Repository reads fail closed: one malformed persisted row suppresses every
    row rather than letting a partial set enter model-visible push context.
    """

    rows: tuple[PreferenceRecord, ...] = ()
    error_type: Literal["invalid_preference_data"] | None = None
    message: str = ""

    @model_validator(mode="after")
    def _has_one_outcome(self) -> PreferenceReadResult:
        if self.error_type is None:
            return self
        if self.rows:
            raise ValueError("invalid preference data cannot include rows")
        if not self.message:
            raise ValueError("invalid preference data needs a safe message")
        return self
