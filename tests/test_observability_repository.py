"""Contract tests for `planazo.observability.repository`.

Covers `record_agent_run` + `query_agent_runs` roundtrip against a
tmpdir-scoped SQLite DB, the CHECK/UNIQUE/FK boundary locks migration
003 introduces, and the composite index the M6 `/find` reader will
query against (via `EXPLAIN QUERY PLAN`).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planazo.identity import get_or_create_user
from planazo.observability import AgentRunRecord, query_agent_runs, record_agent_run
from planazo.storage import db


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sqlite3.Connection:
    """A tmpdir-scoped DB brought forward through every migration + a seeded user."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    conn = db.connect()
    # Seed one users row so the FK on `agent_runs.user_id` resolves.
    get_or_create_user(conn, "tg-1", "Test User")
    return conn


def _make_record(**overrides: object) -> AgentRunRecord:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": "run-abc",
        "agent_kind": "recommender",
        "user_id": 1,
        "user_query": "find techno",
        "final_answer": "here are three",
        "stopped": "answered",
        "steps_count": 3,
        "started_at": now,
        "ended_at": now + timedelta(seconds=5),
    }
    base.update(overrides)
    return AgentRunRecord(**base)  # type: ignore[arg-type]


def test_migration_003_lands_agent_runs_table(isolated_db: sqlite3.Connection) -> None:
    """`user_version` reaches 3 and the table + index exist."""
    version = int(isolated_db.execute("PRAGMA user_version").fetchone()[0])
    assert version >= 3

    tables = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "agent_runs" in tables

    indexes = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_agent_runs_user_started" in indexes


def test_record_and_query_roundtrip(isolated_db: sqlite3.Connection) -> None:
    """One-in-one-out — every field survives the DB roundtrip."""
    record = _make_record()
    row_id = record_agent_run(isolated_db, record)
    assert row_id > 0

    rows = query_agent_runs(isolated_db, user_id=1)
    assert len(rows) == 1
    restored = rows[0]
    assert restored.run_id == "run-abc"
    assert restored.agent_kind == "recommender"
    assert restored.user_id == 1
    assert restored.user_query == "find techno"
    assert restored.final_answer == "here are three"
    assert restored.stopped == "answered"
    assert restored.steps_count == 3
    assert restored.started_at == record.started_at
    assert restored.ended_at == record.ended_at


def test_query_orders_newest_first(isolated_db: sqlite3.Connection) -> None:
    """`ORDER BY started_at DESC` — the M6 `/find` reader shape."""
    base = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    for i in range(5):
        record_agent_run(
            isolated_db,
            _make_record(
                run_id=f"run-{i}",
                started_at=base + timedelta(minutes=i),
                ended_at=base + timedelta(minutes=i, seconds=1),
            ),
        )

    rows = query_agent_runs(isolated_db, user_id=1)
    assert [r.run_id for r in rows] == ["run-4", "run-3", "run-2", "run-1", "run-0"]


def test_query_filters_by_agent_kind(isolated_db: sqlite3.Connection) -> None:
    record_agent_run(isolated_db, _make_record(run_id="r-rec", agent_kind="recommender"))
    record_agent_run(isolated_db, _make_record(run_id="r-ext", agent_kind="extractor"))

    rec_rows = query_agent_runs(isolated_db, agent_kind="recommender")
    ext_rows = query_agent_runs(isolated_db, agent_kind="extractor")

    assert {r.run_id for r in rec_rows} == {"r-rec"}
    assert {r.run_id for r in ext_rows} == {"r-ext"}


def test_query_limit_caps_result_size(isolated_db: sqlite3.Connection) -> None:
    for i in range(10):
        record_agent_run(isolated_db, _make_record(run_id=f"run-{i}"))
    rows = query_agent_runs(isolated_db, user_id=1, limit=3)
    assert len(rows) == 3


def test_query_rejects_zero_limit(isolated_db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        query_agent_runs(isolated_db, limit=0)


def test_query_returns_empty_list_when_no_rows(isolated_db: sqlite3.Connection) -> None:
    assert query_agent_runs(isolated_db, user_id=1) == []


def test_query_uses_idx_agent_runs_user_started_when_filtering_by_user(
    isolated_db: sqlite3.Connection,
) -> None:
    """`EXPLAIN QUERY PLAN` for a user_id-filtered query names the index.

    Locks the M6 read-side story: the "history for one user, most recent
    first" query goes through the composite index, not a full table
    scan. If the query shape drifts (WHERE clause reorder, ORDER BY
    change) the plan text will fall off the index and this test trips.
    """
    plan = isolated_db.execute(
        "EXPLAIN QUERY PLAN"
        " SELECT run_id FROM agent_runs WHERE user_id = ?"
        " ORDER BY started_at DESC LIMIT 100",
        (1,),
    ).fetchall()
    plan_text = " ".join(str(row["detail"]) for row in plan)
    assert "idx_agent_runs_user_started" in plan_text, (
        f"query plan did not use idx_agent_runs_user_started: {plan_text!r}"
    )


def test_agent_kind_check_constraint_rejects_invalid_value(
    isolated_db: sqlite3.Connection,
) -> None:
    """The DB CHECK is the defense-in-depth lock for callers who bypass Pydantic."""
    with pytest.raises(sqlite3.IntegrityError):
        isolated_db.execute(
            "INSERT INTO agent_runs"
            " (run_id, agent_kind, user_id, user_query, final_answer, stopped,"
            "  steps_count, started_at, ended_at)"
            " VALUES ('r-bad', 'scheduler', 1, 'q', 'a', 'answered', 1, ?, ?)",
            (
                datetime(2026, 7, 28, tzinfo=UTC).isoformat(),
                datetime(2026, 7, 28, tzinfo=UTC).isoformat(),
            ),
        )


def test_run_id_unique_constraint_rejects_duplicate(isolated_db: sqlite3.Connection) -> None:
    """`run_id` is UNIQUE — a collision is a caller bug and surfaces loudly."""
    record_agent_run(isolated_db, _make_record(run_id="dup-id"))
    with pytest.raises(sqlite3.IntegrityError):
        record_agent_run(isolated_db, _make_record(run_id="dup-id"))


def test_user_id_foreign_key_rejects_orphan(isolated_db: sqlite3.Connection) -> None:
    """A `user_id` with no `users` row raises `IntegrityError` (FK enforcement)."""
    with pytest.raises(sqlite3.IntegrityError):
        record_agent_run(isolated_db, _make_record(user_id=9999))


def test_null_user_id_accepted_for_operator_run(isolated_db: sqlite3.Connection) -> None:
    """Operator-triggered runs land with `user_id = NULL` — the FK allows null."""
    record_agent_run(isolated_db, _make_record(user_id=None))
    rows = query_agent_runs(isolated_db)
    assert len(rows) == 1
    assert rows[0].user_id is None
