"""Pydantic v2 boundary models for the event-discovery agent's tools.

AGENTS.md rule 1 requires every LLM tool-call payload to pass through a
Pydantic v2 schema before it reaches persisted state, and to fail with a
typed error state rather than a partial artifact dressed up as valid. These
two models are that boundary for `tools.tools`: a `ValidationError` here
becomes a typed `invalid_event_data` / `invalid_confirmation_data` result in
the tool's return value, never an unhandled exception and never a silently
persisted bad record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
