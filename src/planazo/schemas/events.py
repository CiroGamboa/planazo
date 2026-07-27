"""Pydantic v2 boundary models for the event-discovery agent.

AGENTS.md rule 1 requires every LLM tool-call payload to pass through a
Pydantic v2 schema before it reaches persisted state, and to fail with a
typed error state rather than a partial artifact dressed up as valid.

This module is that boundary layer for two neighbouring surfaces:

- The calendar reference tools — `EventCandidateInput` and
  `CalendarConfirmationInput` validate the payloads
  `save_event_candidate` / `confirm_and_create_calendar_event` receive from
  the LLM (see [`docs/adr/0002-event-tool-contracts-and-approval-gate.md`]).
- The query interpreter — `SearchIntent` is the structured output the
  `planazo.query` interpreter emits from a free-text `/find` query and hands
  the downstream Recommender.

A `ValidationError` at either surface is turned into a typed error branch
(the calendar tools' `invalid_event_data` / `invalid_confirmation_data`
return value; the interpreter's `error_type="interpreter_fallback"`
degraded intent), never an unhandled exception and never a silently
persisted bad record.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EventCategory = Literal["tech", "cultural", "music", "networking", "sports", "other"]
EventSource = Literal["eventbrite", "meetup", "instagram", "manual"]
InvitePolicy = Literal["none", "email_invite"]


class EventCandidateInput(BaseModel):
    """A normalized event candidate, validated before `save_event_candidate` persists it."""

    event_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: EventCategory
    source: EventSource
    start_time: datetime
    location: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class CalendarConfirmationInput(BaseModel):
    """A confirmed calendar action, validated before it is persisted."""

    event_id: str = Field(min_length=1)
    notify_invitees: InvitePolicy
    invitee_emails: tuple[str, ...] = ()

    @field_validator("invitee_emails")
    @classmethod
    def _emails_look_like_emails(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for email in value:
            if "@" not in email:
                raise ValueError(f"not a valid email address: {email!r}")
        return value


class SearchIntent(BaseModel):
    """The interpreter's structured output for a `/find` query.

    Fields are validated at construction: unknown categories, negative
    radius/budget, `end_utc` before `start_utc`, or an `error_type` value
    outside the allowed literal set all raise `ValidationError`. The
    interpreter (`planazo.query.interpret`) catches that failure and returns
    a degraded intent tagged `error_type="interpreter_fallback"`; callers
    branch on `error_type` before reading any other field.
    """

    start_utc: datetime
    end_utc: datetime
    categories: tuple[EventCategory, ...] = ()
    city: str = Field(min_length=1)
    radius_km: float | None = Field(default=None, ge=0)
    budget_cents: int | None = Field(default=None, ge=0)
    error_type: Literal["interpreter_fallback"] | None = None

    @field_validator("start_utc", "end_utc", mode="before")
    @classmethod
    def _normalize_to_aware_utc(cls, value: Any) -> datetime:
        # Whatever shape the LLM returned — ISO-8601 string or `datetime` —
        # collapse to an aware UTC datetime. The field name (`*_utc`) is the
        # authoritative convention, so a naive datetime is stamped UTC rather
        # than rejected; an already-aware datetime is converted so the
        # end-not-before-start check below never trips on a tz mismatch.
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        raise ValueError(f"expected an ISO-8601 string or datetime, got {value!r}")

    @field_validator("categories", mode="before")
    @classmethod
    def _split_csv_and_dedupe(cls, value: Any) -> tuple[str, ...]:
        # The LLM's function-call wire shape is a comma-separated string
        # (`schema_for` cannot express `list[Literal[...]]`); direct
        # construction from Python may pass a tuple or other iterable. Both
        # paths land here — split if needed, strip whitespace, drop empties,
        # dedupe in first-seen order. The `Literal` enforcement runs after
        # this normalizer, so an unknown value still raises.
        if isinstance(value, str):
            raw: Iterable[str] = value.split(",")
        elif isinstance(value, Iterable):
            raw = value
        else:
            raise ValueError(f"categories must be a string or iterable, got {value!r}")
        seen: dict[str, None] = {}
        for item in raw:
            if not isinstance(item, str):
                raise ValueError(f"category must be a string, got {item!r}")
            cleaned = item.strip()
            if not cleaned:
                continue
            seen.setdefault(cleaned, None)
        return tuple(seen)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> SearchIntent:
        if self.end_utc < self.start_utc:
            raise ValueError(
                f"end_utc ({self.end_utc.isoformat()}) must not be "
                f"before start_utc ({self.start_utc.isoformat()})"
            )
        return self
