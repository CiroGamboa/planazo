"""Best-effort semantics for `planazo.observability.logging.AgentRunLogger`.

Rule 4 hook: a raise inside the writer must be swallowed with a WARNING
log line — the caller's control flow is unchanged. Also covers the
happy-path insert against a tmpdir-scoped DB, to prove the writer wires
through to `record_agent_run` on the success branch.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planazo.identity import get_or_create_user
from planazo.observability import AgentRunLogger, AgentRunRecord, query_agent_runs
from planazo.storage import db


def _make_record(**overrides: object) -> AgentRunRecord:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": "run-log",
        "agent_kind": "recommender",
        "user_id": 1,
        "user_query": "find me events",
        "final_answer": "here are two",
        "stopped": "answered",
        "steps_count": 2,
        "started_at": now,
        "ended_at": now + timedelta(seconds=3),
    }
    base.update(overrides)
    return AgentRunRecord(**base)  # type: ignore[arg-type]


def test_record_writes_one_row_on_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Success branch: the row lands, queryable back through the repository."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    seed_conn = db.connect()
    try:
        get_or_create_user(seed_conn, "tg-1", "Test User")
    finally:
        seed_conn.close()

    writer = AgentRunLogger(conn_factory=db.connect)
    writer.record(_make_record())

    conn = db.connect()
    try:
        rows = query_agent_runs(conn, user_id=1)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0].run_id == "run-log"


def test_record_swallows_conn_factory_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A `conn_factory` that raises is logged at WARNING and does not propagate."""

    def _boom() -> sqlite3.Connection:
        raise RuntimeError("simulated: could not open db")

    caplog.set_level(logging.WARNING, logger="planazo.observability.logging")
    writer = AgentRunLogger(conn_factory=_boom)
    writer.record(_make_record())  # no exception propagates

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.logging" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "agent_run_logger write failed" in message
    assert "simulated" in message


def test_record_swallows_record_agent_run_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An FK IntegrityError inside `record_agent_run` is swallowed too.

    We aim the writer at a real DB but pass a record with an unseeded
    `user_id`; the FK fires and the writer swallows the resulting
    `sqlite3.IntegrityError`, logging one WARNING.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    conn = db.connect()
    conn.close()  # migrations applied; no users row seeded.

    caplog.set_level(logging.WARNING, logger="planazo.observability.logging")
    writer = AgentRunLogger(conn_factory=db.connect)
    writer.record(_make_record(user_id=9999))  # unseeded FK target

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.logging" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "agent_run_logger write failed" in warnings[0].getMessage()

    # And no row landed — the FK aborted the INSERT.
    conn = db.connect()
    try:
        rows = query_agent_runs(conn)
    finally:
        conn.close()
    assert rows == []
