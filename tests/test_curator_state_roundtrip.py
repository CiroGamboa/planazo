"""Curator state singleton — migrations 009 + 010 + repository roundtrip.

Locks the invariants ADR 0020 rests on:
- Migration 009 seeds exactly one `curator_state` row with `id=1`.
- Migration 010 extends `agent_runs.agent_kind` CHECK to accept `'curator'`.
- Migration 010 extends `llm_decisions.decision_kind` CHECK to accept
  `'archive'`, `'merge'`, `'update_category'`.
- `get_state` / `upsert_state` roundtrip every field.
- `CHECK (id = 1)` refuses a second row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.curator import CuratorState, get_state, upsert_state
from planazo.storage import db


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_migration_009_creates_seeded_singleton(conn: sqlite3.Connection) -> None:
    """`curator_state` exists, has one row, defaults are zero/NULL."""
    rows = conn.execute("SELECT * FROM curator_state").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["last_run_at"] is None
    assert row["last_success_at"] is None
    assert row["consecutive_failures"] == 0
    assert row["total_archived"] == 0
    assert row["total_merged"] == 0
    assert row["total_categories_fixed"] == 0


def test_curator_state_singleton_check_refuses_a_second_row(conn: sqlite3.Connection) -> None:
    """`CHECK (id = 1)` prevents accidental multi-row inserts."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO curator_state (id) VALUES (2)")


def test_migration_010_extends_agent_kind_check(conn: sqlite3.Connection) -> None:
    """`agent_runs.agent_kind` CHECK now accepts `'curator'`."""
    conn.execute(
        "INSERT INTO agent_runs"
        " (run_id, agent_kind, user_query, stopped, steps_count, started_at, ended_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-1", "curator", "test", "answered", 1, "2026-07-28T12:00", "2026-07-28T12:01"),
    )
    row = conn.execute("SELECT agent_kind FROM agent_runs WHERE run_id = 'run-1'").fetchone()
    assert row["agent_kind"] == "curator"


def test_migration_010_extends_decision_kind_check(conn: sqlite3.Connection) -> None:
    """`llm_decisions.decision_kind` CHECK accepts the three curator kinds."""
    conn.execute(
        "INSERT INTO agent_runs"
        " (run_id, agent_kind, user_query, stopped, steps_count, started_at, ended_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-x", "curator", "t", "answered", 1, "2026-07-28T12:00", "2026-07-28T12:01"),
    )
    for kind in ("archive", "merge", "update_category"):
        conn.execute(
            "INSERT INTO llm_decisions"
            " (run_id, decision_kind, rationale, recorded_at)"
            " VALUES (?, ?, ?, ?)",
            ("run-x", kind, "r", "2026-07-28T12:00:30"),
        )
    kinds = {
        row["decision_kind"]
        for row in conn.execute(
            "SELECT decision_kind FROM llm_decisions WHERE run_id = 'run-x'"
        ).fetchall()
    }
    assert kinds == {"archive", "merge", "update_category"}


def test_migration_010_still_rejects_unknown_agent_kind(conn: sqlite3.Connection) -> None:
    """The extended CHECK is not open-ended; a new kind still fails."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_runs"
            " (run_id, agent_kind, user_query, stopped, steps_count, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-2", "admin", "t", "answered", 1, "2026-07-28T12:00", "2026-07-28T12:01"),
        )


def test_get_state_reads_defaults_on_fresh_db(conn: sqlite3.Connection) -> None:
    state = get_state(conn)

    assert state.id == 1
    assert state.last_run_at is None
    assert state.last_success_at is None
    assert state.consecutive_failures == 0
    assert state.total_archived == 0
    assert state.total_merged == 0
    assert state.total_categories_fixed == 0


def test_upsert_state_and_get_state_roundtrip(conn: sqlite3.Connection) -> None:
    stamped_run = datetime(2026, 7, 28, 3, 0, tzinfo=UTC)
    stamped_success = datetime(2026, 7, 28, 3, 1, tzinfo=UTC)
    written = CuratorState(
        last_run_at=stamped_run,
        last_success_at=stamped_success,
        consecutive_failures=0,
        total_archived=3,
        total_merged=1,
        total_categories_fixed=2,
    )

    upsert_state(conn, written)
    read_back = get_state(conn)

    assert read_back.last_run_at == stamped_run
    assert read_back.last_success_at == stamped_success
    assert read_back.total_archived == 3
    assert read_back.total_merged == 1
    assert read_back.total_categories_fixed == 2


def test_upsert_state_overwrites_prior_values(conn: sqlite3.Connection) -> None:
    upsert_state(
        conn,
        CuratorState(
            last_run_at=datetime(2026, 7, 27, 3, 0, tzinfo=UTC),
            consecutive_failures=1,
            total_archived=10,
        ),
    )

    upsert_state(
        conn,
        CuratorState(
            last_run_at=datetime(2026, 7, 28, 3, 0, tzinfo=UTC),
            consecutive_failures=0,
            total_archived=12,
        ),
    )

    state = get_state(conn)
    assert state.last_run_at == datetime(2026, 7, 28, 3, 0, tzinfo=UTC)
    assert state.consecutive_failures == 0
    assert state.total_archived == 12


def test_curator_state_id_locked_to_one_at_pydantic_boundary() -> None:
    """`id` is `ge=1 le=1` — Pydantic refuses anything other than 1."""
    with pytest.raises(ValidationError):
        CuratorState(id=2)
    with pytest.raises(ValidationError):
        CuratorState(id=0)


def test_curator_state_counters_never_negative_at_pydantic_boundary() -> None:
    with pytest.raises(ValidationError):
        CuratorState(total_archived=-1)
    with pytest.raises(ValidationError):
        CuratorState(consecutive_failures=-1)


def test_get_state_raises_when_singleton_row_is_missing(conn: sqlite3.Connection) -> None:
    """A corrupted DB (row deleted out-of-band) raises rather than returning `None`."""
    conn.execute("DELETE FROM curator_state WHERE id = 1")

    with pytest.raises(sqlite3.DatabaseError, match="curator_state row is missing"):
        get_state(conn)
