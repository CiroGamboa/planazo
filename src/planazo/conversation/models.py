"""Pydantic v2 aggregates for the conversation bounded context.

`ConversationState` mirrors the `conversation_state` row 1:1 — one row
per user, upserted every message. `PendingClarification` is the JSON
payload stored in the `pending_clarification` TEXT column: it is
written when the Recommender returns `status="needs_clarification"`
and cleared once the next user message is consumed as the answer.

`ConversationReply` is the service's return type — a discriminated
union in shape (branched on `kind`) that the calling surface (bot
handler, CLI helper, tests) reads to render the outbound message. It
never leaves the `conversation/` context as a `dict`; every surface
projects it into whatever transport-specific text or object it needs.

Every free-form text field (`ConversationReply.answer` and
`ConversationReply.question`) is bounded by a `max_length` so a
malformed downstream projection cannot blow past a Telegram-message
size. `PendingClarification.question` carries the same 500-char cap
as `ClarificationRequest.question` so a snapshot roundtrips through
JSON without a boundary shift.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from planazo.catalog.models import Event
from planazo.query.models import SearchIntent


class PendingClarification(BaseModel):
    """One clarification question the service has asked the user.

    Written to `conversation_state.pending_clarification` (as JSON via
    `model_dump_json`) when the Recommender returns
    `RecommenderResult.status == "needs_clarification"`. Cleared to
    `None` once the user's next message is consumed as the answer.

    `question` is the exact string the Recommender's `ask_user` tool
    recorded (bounded by `ClarificationRequest.question`'s 500-char
    cap on the way in). `intent_snapshot` is the `SearchIntent` the
    service asked about — kept so the follow-up path can rebuild and
    augment the intent when the user answers.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    intent_snapshot: SearchIntent


class ConversationState(BaseModel):
    """One `conversation_state` row — the per-user multi-turn scratchpad.

    Field-for-field mirror of the SQL schema in
    `planazo/storage/migrations/006_conversation_state.sql`. Both
    optional fields (`pending_clarification`, `last_recommendation_run_id`)
    are `None` when no clarification is in flight / no prior
    recommendations run exists.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=1)
    pending_clarification: PendingClarification | None = None
    last_recommendation_run_id: str | None = None
    updated_at: datetime


ConversationReplyKind = Literal["clarification", "recommendations", "detail", "no_results", "error"]
"""The five branches the service's return value takes.

- `clarification` — the Recommender asked a follow-up question. The
  service persisted `pending_clarification`; the caller renders the
  `question` field.
- `recommendations` — the Recommender surfaced 1..N candidates. The
  caller renders `candidates` as a numbered list.
- `detail` — the user asked "tell me about #N" while a
  `last_recommendation_run_id` was active. The caller renders `event`
  as a detail card.
- `no_results` — the Recommender ran but found nothing (fresh query
  or "more results" that filtered every prior candidate). The caller
  renders `answer` — a bounded free-form explanation.
- `error` — the Recommender returned a typed error. The caller
  renders a short human message keyed on `error_type`.
"""


class ConversationReply(BaseModel):
    """The service's return shape — projected onto whatever transport calls it.

    A discriminated union in shape rather than syntax: the caller
    branches on `kind` and reads only the fields relevant to that
    branch. Everything else defaults to `None` / empty. `answer` is a
    free-form field callers use for `no_results` explanations; the
    other branches carry structured artifacts (`candidates`, `event`,
    `question`).

    `candidates` is `tuple[Event, ...]` for immutability at the
    boundary — same shape as `RecommenderResult.candidates`. `event`
    is the single `Event` a "tell me about #N" lookup resolved to.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ConversationReplyKind
    question: str | None = Field(default=None, max_length=500)
    candidates: tuple[Event, ...] = Field(default=(), max_length=100)
    event: Event | None = None
    answer: str | None = Field(default=None, max_length=2_000)
    error_type: str | None = Field(default=None, max_length=200)
