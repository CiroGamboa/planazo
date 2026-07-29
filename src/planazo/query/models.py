"""Pydantic v2 boundary model for the natural-language query interpreter.

`SearchIntent` is the structured output `planazo.query.interpret()` emits from
a free-text `/find` query and hands the downstream Recommender. A
`ValidationError` at construction is turned by `interpret` into a degraded
intent tagged `error_type="interpreter_fallback"` — never an unhandled
exception, never a silently coerced success.

`EventCategory` is the shared literal both this model and the catalog's
`Event` use when discussing event kinds. It lives here because the query
interpreter is the authoritative writer that constrains what the LLM may
produce; catalog consumers accept the same enum by convention.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EventCategory = Literal["tech", "cultural", "music", "networking", "sports", "other"]


class SearchOrigin(BaseModel):
    """Validated geographic origin supplied only by application-owned code."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


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
    radius_km: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_cents: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=50)
    origin: SearchOrigin | None = None
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


def with_search_origin(intent: SearchIntent, origin: SearchOrigin) -> SearchIntent:
    """Return a validated copy of ``intent`` with an application-owned origin."""
    return SearchIntent.model_validate({**intent.model_dump(), "origin": origin})


CHAT_REPLY_MIN_LENGTH = 1
CHAT_REPLY_MAX_LENGTH = 500
"""Bounds on `ChatRoute.answer` — the LLM's own chit-chat / meta-question reply.

Kept tight so a runaway LLM cannot dump paragraphs into the bot surface, and
so a caller-side transport (Telegram, CLI) can render the reply as one message
without size-splitting logic. Matches `ClarificationRequest.question`'s
500-char cap for symmetry (both are LLM-produced short-form text).
"""


class ChatRoute(BaseModel):
    """The interpreter routed the message as small-talk or a meta-question.

    Carries the LLM's own concise reply. `handle_user_message` returns
    this as `ConversationReply(kind="chat", answer=...)` without opening
    a Recommender loop — the tick pays zero `agent_runs`, no
    `recommendations`, no `llm_decisions` rows for that turn.

    ADR 0020 §D3: the interpreter's fallback never lands here. On any
    LLM failure the interpreter returns a `SearchRoute` tagged
    `interpreter_fallback` — the display layer signals uncertainty via
    that tag, not by a fake `chat` reply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["chat"] = "chat"
    answer: str = Field(min_length=CHAT_REPLY_MIN_LENGTH, max_length=CHAT_REPLY_MAX_LENGTH)


class SearchRoute(BaseModel):
    """The interpreter routed the message as a search query.

    Wraps today's `SearchIntent`. `handle_user_message` dispatches to
    `run_once` as it always has. `intent.error_type == "interpreter_fallback"`
    is the tag callers still branch on to render an uncertainty hint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["search"] = "search"
    intent: SearchIntent


RoutedMessage = Annotated[ChatRoute | SearchRoute, Field(discriminator="kind")]
"""Discriminated union `interpret(text)` returns. See ADR 0020.

Two variants:
- `ChatRoute(kind="chat", answer)` — small-talk or meta-question. `answer`
  is 1..500 chars of LLM-produced text. Never opens a Recommender loop.
- `SearchRoute(kind="search", intent)` — the interpreter parsed a search
  query. `intent` is today's `SearchIntent`; the run continues through
  `run_once` unchanged.

Callers dispatch on `.kind` (Pydantic-native discriminator) — the union
is Pydantic-v2 discriminated so a JSON payload round-trips into the
right variant without a manual `isinstance` check at the boundary.
"""
