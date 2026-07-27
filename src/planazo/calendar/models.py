"""Pydantic v2 boundary models for the calendar reference tools.

AGENTS.md rule 1 requires every LLM tool-call payload to pass through a
Pydantic v2 schema before it reaches persisted state, and to fail with a
typed error state rather than a partial artifact dressed up as valid.
`EventCandidateInput` validates the payload `save_event_candidate` receives;
`CalendarConfirmationInput` validates `confirm_and_create_calendar_event`'s.
A `ValidationError` at either becomes an `invalid_event_data` /
`invalid_confirmation_data` typed error branch, never a partial row.

The tool implementations themselves currently live at `src/tools/tools.py`
(ADR 0002); they migrate into this package alongside the real Google
Calendar wiring in a future v0.2 ticket.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from planazo.query.models import EventCategory

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
