"""Contract tests for `planazo.conversation.repository`.

Covers migration 006, `ConversationState` upsert/read round-trip
through SQLite, FK enforcement (a `user_id` with no `users` row must
raise), and the JSON round-trip that `pending_clarification` uses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from planazo.conversation.models import ConversationState, PendingClarification
from planazo.conversation.repository import get_state, upsert_state
from planazo.identity import get_or_create_user
from planazo.query.models import SearchIntent
from planazo.storage import db


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("music",),
    )


def _seed_user(conn: sqlite3.Connection, telegram_user_id: str = "tg-1") -> int:
    record = get_or_create_user(conn, telegram_user_id, "Test User")
    assert record.id is not None
    return record.id


def test_get_state_returns_none_for_absent_user(conn: sqlite3.Connection) -> None:
    user_id = _seed_user(conn)
    assert get_state(conn, user_id) is None


def test_upsert_then_read_round_trips_a_bare_state(conn: sqlite3.Connection) -> None:
    user_id = _seed_user(conn)
    original = ConversationState(
        user_id=user_id,
        pending_clarification=None,
        last_recommendation_run_id="run-a",
        updated_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )
    upsert_state(conn, original)
    read_back = get_state(conn, user_id)
    assert read_back == original


def test_upsert_replaces_the_row_on_second_call(conn: sqlite3.Connection) -> None:
    """One row per user — the second upsert overwrites the first in place."""
    user_id = _seed_user(conn)
    first = ConversationState(
        user_id=user_id,
        last_recommendation_run_id="run-a",
        updated_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )
    upsert_state(conn, first)
    second = ConversationState(
        user_id=user_id,
        last_recommendation_run_id="run-b",
        updated_at=datetime(2026, 7, 29, 13, 0, tzinfo=UTC),
    )
    upsert_state(conn, second)

    read_back = get_state(conn, user_id)
    assert read_back == second
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM conversation_state WHERE user_id = ?", (user_id,)
    ).fetchone()
    assert rows["n"] == 1


def test_pending_clarification_round_trips_through_json_column(
    conn: sqlite3.Connection,
) -> None:
    user_id = _seed_user(conn)
    pending = PendingClarification(question="Which category?", intent_snapshot=_intent())
    state = ConversationState(
        user_id=user_id,
        pending_clarification=pending,
        updated_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )
    upsert_state(conn, state)
    read_back = get_state(conn, user_id)
    assert read_back is not None
    assert read_back.pending_clarification == pending


def test_pending_clarification_cleared_to_none_reads_as_none(
    conn: sqlite3.Connection,
) -> None:
    """Second upsert with `pending_clarification=None` clears the column."""
    user_id = _seed_user(conn)
    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            pending_clarification=PendingClarification(
                question="Which city?", intent_snapshot=_intent()
            ),
            updated_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        ),
    )
    upsert_state(
        conn,
        ConversationState(
            user_id=user_id,
            pending_clarification=None,
            updated_at=datetime(2026, 7, 29, 13, 0, tzinfo=UTC),
        ),
    )
    read_back = get_state(conn, user_id)
    assert read_back is not None
    assert read_back.pending_clarification is None


def test_upsert_with_missing_user_id_raises_integrity_error(
    conn: sqlite3.Connection,
) -> None:
    """FK enforcement — `PRAGMA foreign_keys = ON` at `db.connect()`."""
    state = ConversationState(
        user_id=999,  # not in `users`
        updated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    with pytest.raises(sqlite3.IntegrityError):
        upsert_state(conn, state)
