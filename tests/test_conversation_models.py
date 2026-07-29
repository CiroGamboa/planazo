"""Contract tests for `planazo.conversation.models`.

Locks: construction from valid kwargs, `PendingClarification.model_dump_json()`
round-trip through `PendingClarification.model_validate_json()`,
`ConversationState.pending_clarification` optional-field discipline,
`ConversationReply` kind values, and the required fields per branch.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.conversation.models import (
    ConversationReply,
    ConversationState,
    PendingClarification,
)
from planazo.query.models import SearchIntent


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("music",),
    )


def _event() -> Event:
    return Event(
        id=1,
        source="seed",
        source_url="seed://event/1",
        title="Live jazz",
        start_utc=datetime(2026, 8, 1, 20, tzinfo=UTC),
        end_utc=datetime(2026, 8, 1, 22, tzinfo=UTC),
        category="music",
        city="Barcelona",
        price_cents=1500,
        confidence=0.9,
    )


def test_pending_clarification_constructs_from_valid_kwargs() -> None:
    pending = PendingClarification(question="Which category?", intent_snapshot=_intent())
    assert pending.question == "Which category?"
    assert pending.intent_snapshot.city == "Barcelona"


def test_pending_clarification_requires_question() -> None:
    with pytest.raises(ValidationError):
        PendingClarification(question="", intent_snapshot=_intent())


def test_pending_clarification_round_trips_through_json() -> None:
    """The wire shape stored in `conversation_state.pending_clarification`."""
    original = PendingClarification(question="Music or tech?", intent_snapshot=_intent())
    payload = original.model_dump_json()
    reconstructed = PendingClarification.model_validate_json(payload)
    assert reconstructed == original


def test_conversation_state_constructs_from_valid_kwargs() -> None:
    state = ConversationState(
        user_id=1,
        pending_clarification=None,
        last_recommendation_run_id="run-a",
        updated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    assert state.user_id == 1
    assert state.pending_clarification is None
    assert state.last_recommendation_run_id == "run-a"


def test_conversation_state_rejects_zero_user_id() -> None:
    with pytest.raises(ValidationError):
        ConversationState(
            user_id=0,
            updated_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


def test_conversation_state_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ConversationState(
            user_id=1,
            updated_at=datetime(2026, 7, 29, tzinfo=UTC),
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_conversation_reply_recommendations_carries_candidates() -> None:
    reply = ConversationReply(kind="recommendations", candidates=(_event(),))
    assert reply.kind == "recommendations"
    assert reply.candidates[0].title == "Live jazz"
    assert reply.question is None
    assert reply.event is None


def test_conversation_reply_clarification_carries_question() -> None:
    reply = ConversationReply(kind="clarification", question="Which city?")
    assert reply.kind == "clarification"
    assert reply.question == "Which city?"


def test_conversation_reply_detail_carries_event() -> None:
    event = _event()
    reply = ConversationReply(kind="detail", event=event, answer="Live jazz · ...")
    assert reply.kind == "detail"
    assert reply.event is event


def test_conversation_reply_error_carries_error_type() -> None:
    reply = ConversationReply(kind="error", error_type="search_tool_failure")
    assert reply.error_type == "search_tool_failure"


def test_conversation_reply_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ConversationReply(kind="nonsense")  # type: ignore[arg-type]
