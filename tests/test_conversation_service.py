"""Behavior tests for `planazo.conversation.service.handle_user_message`.

Every test drives the real service composition root against a real
`:memory:` SQLite DB — `run_once` and `interpret` are the only
mocked seams (their real invocations would call the LLM). Covers:

- Fresh query, no state → run_once dispatched with the interpreted intent.
- `needs_clarification` return → `pending_clarification` persisted;
  reply.kind == "clarification".
- Clarification-answer path → `pending_clarification` cleared; one
  `PreferenceRecord` written under a `pref:clarified.<key>` namespace;
  re-runs the search.
- "tell me about 2" → returns the 2nd rec's Event via `get_event_by_id`.
- Numeric-only "2" with active `last_recommendation_run_id` → same detail.
- "more results" → re-runs with the intent from the prior run's
  `agent_runs.user_query`; filters out event IDs already surfaced.
- "more results" with every candidate already shown → `kind="no_results"`.
- Error branch (`status="error"`) → `kind="error"` with error_type.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from planazo.agents.event_agent import ClarificationRequest, RecommenderResult
from planazo.catalog.models import Event
from planazo.catalog.repository import insert_event
from planazo.conversation import service
from planazo.conversation.repository import get_state
from planazo.identity import get_or_create_user, get_preferences
from planazo.observability import (
    FINAL_ANSWER_CAP,
    USER_QUERY_CAP,
    AgentRunRecord,
    format_stored_text,
)
from planazo.observability.repository import (
    record_agent_run,
    record_recommendations,
)
from planazo.query.models import SearchIntent
from planazo.storage import db

# ---------- fixtures --------------------------------------------------------


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[sqlite3.Connection]:
    """A `tmp_path` file DB so `service._connect_for_lookup()` sees the same rows.

    The service opens its own connection through `db.connect()` for
    the run_id lookup after `run_once` returns. Using a file DB
    (not `:memory:`) lets the second `db.connect()` see rows written
    by the test's primary connection.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def user_id(conn: sqlite3.Connection) -> int:
    record = get_or_create_user(conn, "tg-1", "E2E User")
    assert record.id is not None
    return record.id


def _intent(**overrides: Any) -> SearchIntent:
    values: dict[str, Any] = {
        "start_utc": datetime(2026, 8, 1, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 2, tzinfo=UTC),
        "city": "Barcelona",
        "categories": ("music",),
    }
    values.update(overrides)
    return SearchIntent(**values)


def _event(**overrides: Any) -> Event:
    values: dict[str, Any] = {
        "source": "seed",
        "source_url": f"seed://event/{overrides.get('title', 'x')}",
        "title": "Ev",
        "start_utc": datetime(2026, 8, 1, 20, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 22, tzinfo=UTC),
        "category": "music",
        "city": "Barcelona",
        "price_cents": 0,
        "confidence": 0.9,
    }
    values.update(overrides)
    return Event(**values)


def _seed_event(conn: sqlite3.Connection, **overrides: Any) -> Event:
    event = _event(**overrides)
    event_id = insert_event(conn, event)
    return event.model_copy(update={"id": event_id})


def _record_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    user_id: int,
    intent: SearchIntent,
) -> None:
    """Persist one `agent_runs` row + recommendations."""
    now = datetime.now(UTC)
    record = AgentRunRecord(
        run_id=run_id,
        agent_kind="recommender",
        user_id=user_id,
        user_query=format_stored_text(intent.model_dump_json(), USER_QUERY_CAP),
        final_answer=format_stored_text("done", FINAL_ANSWER_CAP),
        stopped="answered",
        steps_count=1,
        started_at=now,
        ended_at=now,
    )
    record_agent_run(conn, record)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    interpret_returns: SearchIntent,
    run_once_returns: RecommenderResult | list[RecommenderResult],
    run_id: str | list[str] = "run-generated",
) -> tuple[list[tuple[int, SearchIntent]], list[str]]:
    """Stub out `interpret` and `_run_and_capture` in the service module.

    `_run_and_capture` wraps `run_once` and the run_id lookup — this
    stub replaces the pair so tests don't have to also stand up the
    audit-store lookup path. The `run_id` returned matches what the
    caller passed; a `list` lets one test drive multiple run_ids
    through the same `handle_user_message` chain.
    """
    calls: list[tuple[int, SearchIntent]] = []
    results = run_once_returns if isinstance(run_once_returns, list) else [run_once_returns]
    run_ids = run_id if isinstance(run_id, list) else [run_id]
    result_iter = iter(results)
    run_id_iter = iter(run_ids)

    from planazo.query.models import SearchRoute

    def fake_interpret(text: str) -> SearchRoute:
        # ADR 0020: `interpret` now returns a discriminated `RoutedMessage`.
        # Tests targeting the search branch wrap the mock intent here so the
        # fresh-query path unwraps it back to `interpret_returns`.
        return SearchRoute(intent=interpret_returns)

    def fake_interpret_search_only(text: str) -> SearchIntent:
        return interpret_returns

    def fake_run_and_capture(
        user_id: int, intent: SearchIntent
    ) -> tuple[RecommenderResult, str | None]:
        calls.append((user_id, intent))
        return next(result_iter), next(run_id_iter, None)

    monkeypatch.setattr(service, "interpret", fake_interpret)
    monkeypatch.setattr(service, "interpret_search_only", fake_interpret_search_only)
    monkeypatch.setattr(service, "_run_and_capture", fake_run_and_capture)
    return calls, run_ids


# ---------- tests -----------------------------------------------------------


def test_fresh_query_with_no_state_runs_interpret_and_run_once(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    intent = _intent()
    event = _seed_event(conn, title="Concert A", source_url="seed://event/a")
    calls, _ = _install_stubs(
        monkeypatch,
        interpret_returns=intent,
        run_once_returns=RecommenderResult(
            status="ok", answer="done", stopped="answered", steps=1, candidates=(event,)
        ),
        run_id="run-1",
    )
    # An audit row must exist so `_run_and_capture` can look up run_id
    # — but here we stub `_run_and_capture` directly, so it's not needed.

    reply = service.handle_user_message(conn, user_id, "music tonight")

    assert reply.kind == "recommendations"
    assert reply.candidates == (event,)
    assert calls == [(user_id, intent)]

    persisted = get_state(conn, user_id)
    assert persisted is not None
    assert persisted.last_recommendation_run_id == "run-1"
    assert persisted.pending_clarification is None


def test_needs_clarification_persists_pending_and_returns_kind_clarification(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    intent = _intent()
    _install_stubs(
        monkeypatch,
        interpret_returns=intent,
        run_once_returns=RecommenderResult(
            status="needs_clarification",
            answer=None,
            stopped="answered",
            steps=1,
            clarification=ClarificationRequest(question="Which category?"),
        ),
        run_id="run-x",
    )

    reply = service.handle_user_message(conn, user_id, "find me stuff")

    assert reply.kind == "clarification"
    assert reply.question == "Which category?"

    persisted = get_state(conn, user_id)
    assert persisted is not None
    assert persisted.pending_clarification is not None
    assert persisted.pending_clarification.question == "Which category?"
    assert persisted.pending_clarification.intent_snapshot == intent


def test_clarification_answer_writes_preference_and_reruns(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    # Turn 1: Recommender asks for clarification.
    intent1 = _intent()
    intent2 = _intent(categories=("music",))
    ev = _seed_event(conn, title="Concert B", source_url="seed://event/b")
    _install_stubs(
        monkeypatch,
        interpret_returns=intent1,
        run_once_returns=[
            RecommenderResult(
                status="needs_clarification",
                answer=None,
                stopped="answered",
                steps=1,
                clarification=ClarificationRequest(question="Which category?"),
            ),
            RecommenderResult(
                status="ok",
                answer="done",
                stopped="answered",
                steps=1,
                candidates=(ev,),
            ),
        ],
        run_id=["run-clar", "run-answer"],
    )

    # First: triggers pending_clarification.
    service.handle_user_message(conn, user_id, "find me events")

    # Now stub `interpret_search_only` for turn 2 (music enrichment).
    # ADR 0020 §D5: the clarification-answer path bypasses the router.
    monkeypatch.setattr(service, "interpret_search_only", lambda _text: intent2)

    reply = service.handle_user_message(conn, user_id, "music")

    assert reply.kind == "recommendations"
    assert reply.candidates == (ev,)

    prefs = get_preferences(conn, user_id)
    assert prefs.error_type is None
    matching = [row for row in prefs.rows if row.key.startswith("pref:clarified.")]
    assert len(matching) == 1
    assert matching[0].key == "pref:clarified.categories"
    assert matching[0].value == "music"

    persisted = get_state(conn, user_id)
    assert persisted is not None
    assert persisted.pending_clarification is None
    assert persisted.last_recommendation_run_id == "run-answer"


def test_tell_me_about_2_returns_the_second_recommendation(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    # Prime state: a prior run with two recommendations.
    ev1 = _seed_event(conn, title="Concert A", source_url="seed://event/a")
    ev2 = _seed_event(conn, title="Concert B", source_url="seed://event/b")
    intent = _intent()
    _record_run(conn, run_id="run-prior", user_id=user_id, intent=intent)
    record_recommendations(conn, "run-prior", [ev1, ev2])
    from planazo.conversation.models import ConversationState
    from planazo.conversation.repository import upsert_state

    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            last_recommendation_run_id="run-prior",
            updated_at=datetime.now(UTC),
        ),
    )

    # No stubs needed — this branch never calls run_once/interpret.
    reply = service.handle_user_message(conn, user_id, "tell me about 2")

    assert reply.kind == "detail"
    assert reply.event is not None
    assert reply.event.id == ev2.id
    assert reply.event.title == "Concert B"


def test_bare_numeric_answer_with_active_run_returns_detail(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    ev1 = _seed_event(conn, title="Concert A", source_url="seed://event/a")
    ev2 = _seed_event(conn, title="Concert B", source_url="seed://event/b")
    _record_run(conn, run_id="run-prior", user_id=user_id, intent=_intent())
    record_recommendations(conn, "run-prior", [ev1, ev2])
    from planazo.conversation.models import ConversationState
    from planazo.conversation.repository import upsert_state

    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            last_recommendation_run_id="run-prior",
            updated_at=datetime.now(UTC),
        ),
    )

    reply = service.handle_user_message(conn, user_id, "1")

    assert reply.kind == "detail"
    assert reply.event is not None
    assert reply.event.id == ev1.id


def test_more_results_reruns_and_filters_prior_event_ids(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    # Prime the DB: prior run with one recommendation.
    ev1 = _seed_event(conn, title="Concert A", source_url="seed://event/a")
    ev2 = _seed_event(conn, title="Concert B", source_url="seed://event/b")
    intent = _intent()
    _record_run(conn, run_id="run-prior", user_id=user_id, intent=intent)
    record_recommendations(conn, "run-prior", [ev1])
    from planazo.conversation.models import ConversationState
    from planazo.conversation.repository import upsert_state

    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            last_recommendation_run_id="run-prior",
            updated_at=datetime.now(UTC),
        ),
    )

    # Rerun returns both ev1 (already shown) and ev2 (new).
    _install_stubs(
        monkeypatch,
        interpret_returns=intent,
        run_once_returns=RecommenderResult(
            status="ok",
            answer="done",
            stopped="answered",
            steps=1,
            candidates=(ev1, ev2),
        ),
        run_id="run-more",
    )

    reply = service.handle_user_message(conn, user_id, "more results")

    assert reply.kind == "recommendations"
    assert len(reply.candidates) == 1
    assert reply.candidates[0].id == ev2.id


def test_more_results_with_every_candidate_already_shown_returns_no_results(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    ev = _seed_event(conn, title="Concert A", source_url="seed://event/a")
    intent = _intent()
    _record_run(conn, run_id="run-prior", user_id=user_id, intent=intent)
    record_recommendations(conn, "run-prior", [ev])
    from planazo.conversation.models import ConversationState
    from planazo.conversation.repository import upsert_state

    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            last_recommendation_run_id="run-prior",
            updated_at=datetime.now(UTC),
        ),
    )
    _install_stubs(
        monkeypatch,
        interpret_returns=intent,
        run_once_returns=RecommenderResult(
            status="ok",
            answer="done",
            stopped="answered",
            steps=1,
            candidates=(ev,),
        ),
        run_id="run-more",
    )

    reply = service.handle_user_message(conn, user_id, "more")

    assert reply.kind == "no_results"
    assert "already been shown" in (reply.answer or "").lower()


def test_error_branch_returns_kind_error(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    _install_stubs(
        monkeypatch,
        interpret_returns=_intent(),
        run_once_returns=RecommenderResult(
            status="error",
            answer="err",
            stopped="answered",
            steps=1,
            error_type="search_tool_failure",
        ),
        run_id="run-err",
    )

    reply = service.handle_user_message(conn, user_id, "anything")

    assert reply.kind == "error"
    assert reply.error_type == "search_tool_failure"


def test_first_time_user_no_state_still_returns_reply(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    """A user with no `conversation_state` row falls through to fresh path."""
    _install_stubs(
        monkeypatch,
        interpret_returns=_intent(),
        run_once_returns=RecommenderResult(
            status="no_results", answer="none found", stopped="answered", steps=1
        ),
        run_id="run-none",
    )

    assert get_state(conn, user_id) is None

    reply = service.handle_user_message(conn, user_id, "obscure query")

    assert reply.kind == "no_results"
    assert get_state(conn, user_id) is not None


def test_more_results_without_prior_run_falls_through_to_fresh(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    """Text `"more"` with no `last_recommendation_run_id` is treated as fresh.

    The service's precedence rules require a `run_id` to be set
    before either follow-up pattern fires; without it the message is
    just another fresh query.
    """
    intent = _intent()
    _install_stubs(
        monkeypatch,
        interpret_returns=intent,
        run_once_returns=RecommenderResult(
            status="no_results", answer="none", stopped="answered", steps=1
        ),
        run_id="run-fresh",
    )

    reply = service.handle_user_message(conn, user_id, "more")
    assert reply.kind == "no_results"


def test_chat_route_returns_kind_chat_without_calling_run_once(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    """ADR 0020: a greeting routed by the interpreter as `chat` skips `run_once`.

    No `agent_runs` / `recommendations` / `llm_decisions` row is
    written for this turn. `conversation_state.updated_at` refreshes
    but every other field is preserved.
    """
    from planazo.query.models import ChatRoute

    run_once_calls: list[tuple[int, object]] = []

    def fake_run_and_capture(user_id: int, intent: object) -> tuple[object, str | None]:
        run_once_calls.append((user_id, intent))
        raise AssertionError("run_once must not fire on a chat route")

    monkeypatch.setattr(
        service,
        "interpret",
        lambda _text: ChatRoute(answer="¡Hola! Aquí estoy para recomendarte eventos."),
    )
    monkeypatch.setattr(service, "_run_and_capture", fake_run_and_capture)

    reply = service.handle_user_message(conn, user_id, "Hola")

    assert reply.kind == "chat"
    assert reply.answer == "¡Hola! Aquí estoy para recomendarte eventos."
    assert run_once_calls == []
    # State is refreshed (updated_at) but chat carries no run_id.
    state = get_state(conn, user_id)
    assert state is not None
    assert state.last_recommendation_run_id is None


def test_chat_route_preserves_prior_run_id_and_pending_clarification(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    """A chat turn between search turns doesn't wipe follow-up state.

    ADR 0020 §D5: the router doesn't invalidate ongoing conversation
    threads — a "Hi" in the middle of a `/find` conversation preserves
    the ability to do "more results" or "tell me about #N" on the
    prior batch.
    """
    from planazo.conversation.models import ConversationState
    from planazo.conversation.repository import now_utc, upsert_state
    from planazo.query.models import ChatRoute

    # Seed a state with a prior run_id — simulates a `/find` from the prior turn.
    prior = ConversationState(
        user_id=user_id,
        pending_clarification=None,
        last_recommendation_run_id="run-prior",
        updated_at=now_utc(),
    )
    upsert_state(conn, prior)

    monkeypatch.setattr(
        service, "interpret", lambda _text: ChatRoute(answer="Sure — how can I help?")
    )

    reply = service.handle_user_message(conn, user_id, "thanks!")

    assert reply.kind == "chat"
    state = get_state(conn, user_id)
    assert state is not None
    assert state.last_recommendation_run_id == "run-prior"


def test_clarification_answer_bypasses_router_via_interpret_search_only(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, user_id: int
) -> None:
    """ADR 0020 §D5: mid-clarification, the router is bypassed.

    When a user mid-clarification sends a numeric-only answer or a
    short cue, an over-eager LLM might route it as chat. The
    clarification-answer path is the more specific state and wins —
    it calls `interpret_search_only` which returns a SearchIntent
    unconditionally, so `run_once` still fires.
    """
    from planazo.conversation.models import ConversationState, PendingClarification
    from planazo.conversation.repository import now_utc, upsert_state
    from planazo.query.models import ChatRoute

    # Seed a pending clarification.
    intent_snapshot = _intent()
    pending = PendingClarification(question="Which day?", intent_snapshot=intent_snapshot)
    prior = ConversationState(
        user_id=user_id,
        pending_clarification=pending,
        last_recommendation_run_id=None,
        updated_at=now_utc(),
    )
    upsert_state(conn, prior)

    # Even though `interpret` would return a chat route for "2",
    # `interpret_search_only` still returns a search intent.
    monkeypatch.setattr(
        service,
        "interpret",
        lambda _text: ChatRoute(answer="Nice number!"),
    )
    fresh_intent = _intent()
    monkeypatch.setattr(service, "interpret_search_only", lambda _text: fresh_intent)

    result = RecommenderResult(status="no_results", answer="none", stopped="answered", steps=1)
    monkeypatch.setattr(service, "_run_and_capture", lambda _uid, _intent: (result, "run-clar-ans"))

    reply = service.handle_user_message(conn, user_id, "2")

    # Clarification-answer path fired → run_once was invoked → reply is `no_results`,
    # NOT `chat`.
    assert reply.kind == "no_results"
