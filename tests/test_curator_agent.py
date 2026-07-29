"""Curator agent — `run_curator_once` + `run_curator` composition.

Every test stubs `run_loop` so no LLM is called. The stub returns a
canned `LoopResult` and drives a scripted trace of `StepRecord`s through
the observer that mimics what a real LLM tick would produce. This
lets us exercise the counters, the `agent_runs` / `llm_decisions`
writers, the `curator_state` upsert, and the audit-log append without
any API key or network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from planazo.agents.loop import LoopResult, StepRecord
from planazo.catalog import Event, insert_event
from planazo.curator import agent as curator_agent
from planazo.curator.agent import CuratorRunResult, run_curator_once
from planazo.curator.repository import get_state
from planazo.curator.service import run_curator
from planazo.storage import db


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "source": "seed",
        "source_url": "https://seed/e/1",
        "title": "AI Meetup",
        "start_utc": datetime(2026, 8, 1, 19, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 21, 0, tzinfo=UTC),
        "category": "tech",
        "city": "Barcelona",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


class _NoCloseConn:
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.fixture
def tmp_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    proxy = _NoCloseConn(connection)
    monkeypatch.setattr(db, "connect", lambda: proxy)
    yield connection
    connection.close()


def _step(step_index: int, tool: str, arguments: dict[str, Any], result: Any) -> StepRecord:
    return StepRecord(
        step=step_index,
        tool=tool,
        arguments=arguments,
        result=result,
    )


def _stub_run_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripted_trace: list[StepRecord],
    stopped: str = "answered",
    steps: int = 3,
    answer: str | None = "done",
) -> None:
    """Replace `run_loop` in `curator.agent` with a scripted stub.

    The stub calls `on_step` for every entry in `scripted_trace` and
    returns a `LoopResult` matching the desired terminal shape. It
    does NOT actually dispatch tools — the trace is a fiction that
    represents what the LLM would have produced.
    """

    def fake_run_loop(**kwargs: Any) -> LoopResult:
        on_step = kwargs.get("on_step")
        if on_step is not None:
            for record in scripted_trace:
                on_step(record)
        return LoopResult(answer=answer, steps=steps, stopped=stopped)  # type: ignore[arg-type]

    monkeypatch.setattr(curator_agent, "run_loop", fake_run_loop)


# ---------------------------------------------------------------------------
# run_curator_once — counters + writer discipline
# ---------------------------------------------------------------------------


def test_run_curator_once_counts_archives_merges_and_updates(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = [
        _step(
            1,
            "list_stale_events",
            {"limit": 50},
            {"events": [{"event_id": 1}, {"event_id": 2}], "total": 2},
        ),
        _step(
            2,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "ok", "archived_event_id": 1},
        ),
        _step(
            3,
            "merge_events",
            {
                "keep_event_id": 3,
                "archive_event_ids": [4, 5],
                "reason": "dupes",
            },
            {"status": "ok", "kept_event_id": 3, "archived_event_ids": [4, 5]},
        ),
        _step(
            4,
            "update_event_category",
            {"event_id": 6, "new_category": "cultural", "reason": "was tech"},
            {"status": "ok", "event_id": 6, "old_category": "tech", "new_category": "cultural"},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace, stopped="answered", steps=4)

    result = run_curator_once(record_runs=False)

    assert isinstance(result, CuratorRunResult)
    assert result.stopped == "answered"
    assert result.steps == 4
    # `events_archived` includes merged rows too (they're all archived).
    assert result.events_archived == 3
    assert result.events_merged == 2
    assert result.categories_updated == 1
    assert result.events_examined == 2
    assert result.errors == []
    assert result.dry_run is False


def test_run_curator_once_collects_write_errors(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 999, "reason": "ghost"},
            {"error_type": "not_found", "message": "no event with id=999"},
        ),
        _step(
            2,
            "merge_events",
            {"keep_event_id": 1, "archive_event_ids": [2], "reason": "test"},
            {"error_type": "already_archived", "message": "event id=2 is already archived"},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)

    result = run_curator_once(record_runs=False)

    assert result.events_archived == 0
    assert result.events_merged == 0
    assert len(result.errors) == 2
    assert any("not_found" in err for err in result.errors)
    assert any("already_archived" in err for err in result.errors)


def test_run_curator_once_collects_tool_failed_markers(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_loop` synthesizes `{"tool_failed": True, "error": "..."}` when a
    write tool raises. `_collect_write_errors` must surface these in
    `CuratorRunResult.errors` — the real-API smoke on 2026-07-29 caught
    this via LLM sending malformed args that tripped a TypeError."""
    trace = [
        _step(
            1,
            "merge_events",
            {"keep_event_id": 1, "archive_event_ids": "26", "reason": "bad shape"},
            {
                "tool_failed": True,
                "error": "TypeError: 'in <string>' requires string as left operand, not int",
            },
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)

    result = run_curator_once(record_runs=False)

    assert len(result.errors) == 1
    assert "tool_failed" in result.errors[0]
    assert "TypeError" in result.errors[0]


def test_run_curator_once_dry_run_marks_result_and_skips_llm_decisions(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run trace returns `status="dry_run"` — no llm_decisions rows fire."""
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "dry_run", "archived_event_id": 1},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)

    result = run_curator_once(dry_run=True, record_runs=True)

    assert result.dry_run is True
    # dry_run tool calls don't count as archives (nothing was archived).
    assert result.events_archived == 0
    # No `llm_decisions` row for a `dry_run` outcome.
    rows = tmp_db.execute(
        "SELECT COUNT(*) FROM llm_decisions WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert rows[0] == 0


def test_run_curator_once_record_runs_false_writes_nothing(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "ok", "archived_event_id": 1},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)

    result = run_curator_once(record_runs=False)

    agent_row = tmp_db.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    decision_row = tmp_db.execute(
        "SELECT COUNT(*) FROM llm_decisions WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert agent_row[0] == 0
    assert decision_row[0] == 0


def test_run_curator_once_record_runs_true_writes_agent_run_and_decisions(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed real events so the FK from llm_decisions.event_db_id resolves.
    id_a = insert_event(tmp_db, make_event(source_url="https://seed/a"))
    id_b = insert_event(tmp_db, make_event(source_url="https://seed/b"))
    id_c = insert_event(tmp_db, make_event(source_url="https://seed/c"))
    id_d = insert_event(tmp_db, make_event(source_url="https://seed/d"))
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": id_a, "reason": "event has ended"},
            {"status": "ok", "archived_event_id": id_a},
        ),
        _step(
            2,
            "merge_events",
            {
                "keep_event_id": id_b,
                "archive_event_ids": [id_c, id_d],
                "reason": "same event",
            },
            {
                "status": "ok",
                "kept_event_id": id_b,
                "archived_event_ids": [id_c, id_d],
            },
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)

    result = run_curator_once(record_runs=True)

    agent_row = tmp_db.execute(
        "SELECT agent_kind, user_id FROM agent_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert agent_row is not None
    assert agent_row["agent_kind"] == "curator"
    assert agent_row["user_id"] is None  # curator is system-owned

    decisions = tmp_db.execute(
        "SELECT decision_kind, event_db_id, rationale FROM llm_decisions"
        " WHERE run_id = ? ORDER BY id",
        (result.run_id,),
    ).fetchall()
    kinds = [row["decision_kind"] for row in decisions]
    event_ids = [row["event_db_id"] for row in decisions]
    assert kinds == ["archive", "merge", "merge"]
    assert event_ids == [id_a, id_c, id_d]
    assert all(
        "event has ended" in row["rationale"] or "same event" in row["rationale"]
        for row in decisions
    )


def test_run_curator_once_writes_loop_terminal_error_row_on_max_steps(
    tmp_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `stopped='max_steps'` run gets one synthetic `error` llm_decisions row."""
    event_id = insert_event(tmp_db, make_event())
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": event_id, "reason": "ok"},
            {"status": "ok", "archived_event_id": event_id},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace, stopped="max_steps", steps=12, answer=None)

    result = run_curator_once(record_runs=True)

    assert result.stopped == "max_steps"
    rows = tmp_db.execute(
        "SELECT decision_kind, error_type FROM llm_decisions WHERE run_id = ? ORDER BY id",
        (result.run_id,),
    ).fetchall()
    kinds = [row["decision_kind"] for row in rows]
    assert "archive" in kinds
    assert "error" in kinds
    error_row = next(row for row in rows if row["decision_kind"] == "error")
    assert error_row["error_type"] == "max_steps"


# ---------------------------------------------------------------------------
# run_curator — composition root
# ---------------------------------------------------------------------------


def test_run_curator_upserts_curator_state_on_success(
    tmp_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "ok", "archived_event_id": 1},
        ),
        _step(
            2,
            "update_event_category",
            {"event_id": 2, "new_category": "cultural", "reason": "mis-classified"},
            {"status": "ok", "event_id": 2, "old_category": "tech", "new_category": "cultural"},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace, stopped="answered")
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_curator(record_runs=False, audit_log_path=audit_log)

    state = get_state(tmp_db)
    assert state.last_run_at == result.ended_at
    assert state.last_success_at == result.ended_at
    assert state.consecutive_failures == 0
    assert state.total_archived == 1
    assert state.total_merged == 0
    assert state.total_categories_fixed == 1


def test_run_curator_bumps_consecutive_failures_on_truncated(
    tmp_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_run_loop(monkeypatch, scripted_trace=[], stopped="truncated", answer=None)
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_curator(record_runs=False, audit_log_path=audit_log)

    state = get_state(tmp_db)
    assert state.consecutive_failures == 1
    assert state.last_run_at == result.ended_at
    assert state.last_success_at is None


def test_run_curator_appends_audit_log_line(
    tmp_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "ok", "archived_event_id": 1},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_curator(record_runs=False, audit_log_path=audit_log)

    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json as _json

    parsed = _json.loads(lines[0])
    assert parsed["run_id"] == result.run_id
    assert parsed["events_archived"] == 1
    assert parsed["dry_run"] is False


def test_run_curator_audit_log_survives_state_upsert_failure(
    tmp_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A `curator_state` upsert failure is swallowed; the audit log still writes."""
    trace = [
        _step(
            1,
            "archive_event",
            {"event_id": 1, "reason": "past"},
            {"status": "ok", "archived_event_id": 1},
        ),
    ]
    _stub_run_loop(monkeypatch, scripted_trace=trace)
    audit_log = tmp_path / "curator_runs.jsonl"

    def bad_connect() -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated conn failure")

    # Compose: real writes go through the fixture's proxy connection; the
    # service-level conn_factory is what upserts curator_state.
    result = run_curator(record_runs=False, audit_log_path=audit_log, conn_factory=bad_connect)

    # The audit log still landed.
    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    # And the result was returned normally.
    assert result.events_archived == 1
