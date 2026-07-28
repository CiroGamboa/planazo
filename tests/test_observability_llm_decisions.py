"""Contract tests for `planazo.observability` — the `llm_decisions` surface.

Locks the four-way `LLMDecision.decision_kind` state machine, the
migration 004 schema, and the composition-root wiring on both loops.
End-to-end tests exercise `extract_once` + `event_agent.run_once`
through a stubbed LLM to prove the rationale rows land alongside the
matching `agent_runs` row (same `run_id`, FK-satisfied).

Best-effort semantics are covered by the last block: a raise inside
`record_llm_decision` from the `_build_result` seam does not propagate.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agentlib.core import CHEAP, STRONG, Result
from planazo.agents import event_agent
from planazo.agents.extractor import extract_once
from planazo.agents.loop import LoopResult
from planazo.identity import get_or_create_user
from planazo.memory import facts, rules
from planazo.observability import (
    RATIONALE_CAP,
    LLMDecision,
    format_stored_text,
    query_agent_runs,
    query_llm_decisions,
    record_llm_decision,
)
from planazo.query.models import SearchIntent
from planazo.sources.config import MediaTypeFlags, SourceConfig
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClientProtocol
from planazo.sources.instagram.model_view import InstaloaderPostView
from planazo.storage import db

_TEST_URL = "https://www.instagram.com/p/ABC123/"


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sqlite3.Connection:
    """A tmpdir-scoped DB brought forward through every migration + a seeded user + run."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    conn = db.connect()
    # Seed one users row so downstream FK targets on `agent_runs.user_id` resolve.
    get_or_create_user(conn, "tg-1", "Test User")
    # Seed one `agent_runs` row so the `llm_decisions.run_id` FK has a target.
    now_iso = datetime(2026, 7, 28, 12, 0, tzinfo=UTC).isoformat()
    conn.execute(
        "INSERT INTO agent_runs"
        " (run_id, agent_kind, user_id, user_query, final_answer, stopped,"
        "  steps_count, started_at, ended_at)"
        " VALUES ('parent-run', 'recommender', 1, 'q', 'a', 'answered', 1, ?, ?)",
        (now_iso, now_iso),
    )
    conn.commit()
    return conn


@pytest.fixture
def isolated_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Rules dir, docstore, DB, and extractor log routed at a test tree."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    monkeypatch.setattr(
        "planazo.extraction.audit.default_extraction_log_path",
        lambda: tmp_path / "extraction_runs.jsonl",
    )
    return tmp_path


def _make_result(**overrides: object) -> Result:
    defaults: dict[str, object] = {
        "text": "ok",
        "model": CHEAP,
        "status": "completed",
        "stop_reason": None,
        "truncated": False,
        "input_tokens": 13,
        "cached_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "reasoning_summary": None,
    }
    defaults.update(overrides)
    return Result(**defaults)  # type: ignore[arg-type]


def _turn(text: str, tool_calls: list[dict[str, Any]] | None = None) -> Result:
    tool_calls = tool_calls or []
    output_items = [
        {
            "type": "function_call",
            "name": tc["name"],
            "arguments": json.dumps(tc["arguments"]),
            "call_id": tc["call_id"],
        }
        for tc in tool_calls
    ]
    return _make_result(text=text, tool_calls=tool_calls, output_items=output_items)


class _FakeInstagramClient:
    def __init__(self, view: InstaloaderPostView) -> None:
        self._view = view

    def fetch_metadata(self, shortcode: str) -> InstaloaderPostView:
        return self._view


def _build_source(caption: str = "come along") -> InstagramSource:
    view = InstaloaderPostView.model_validate(
        {
            "shortcode": "ABC123",
            "typename": "GraphImage",
            "caption": caption,
            "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
            "owner_username": "test_venue",
            "url": "https://scontent.cdninstagram.com/image.jpg",
            "video_url": None,
            "video_duration": None,
            "mediacount": 1,
            "sidecar_nodes": [],
        }
    )
    client: InstagramClientProtocol = _FakeInstagramClient(view)
    config = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[],
    )
    return InstagramSource(config, client)


def _seed_user() -> int:
    conn = db.connect()
    try:
        user = get_or_create_user(conn, "tg-1", "Test User")
        assert user.id is not None
        return user.id
    finally:
        conn.close()


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    """Baseline valid `LLMDecision` kwargs — the four branches override individual fields."""
    base: dict[str, object] = {
        "run_id": "parent-run",
        "decision_kind": "answered",
        "event_db_id": None,
        "error_type": None,
        "rationale": "here are three techno events",
        "recorded_at": datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


# ---- LLMDecision model_validator -------------------------------------------


def test_llm_decision_save_event_accepts_valid_shape() -> None:
    """`save_event` with `event_db_id` set and `error_type=None` — legal."""
    decision = LLMDecision(
        **_valid_kwargs(decision_kind="save_event", event_db_id=42, error_type=None)
    )
    assert decision.decision_kind == "save_event"
    assert decision.event_db_id == 42


def test_llm_decision_save_event_rejects_missing_event_db_id() -> None:
    """`save_event` without `event_db_id` — the row would advertise a nonexistent Event."""
    with pytest.raises(ValidationError, match="event_db_id"):
        LLMDecision(**_valid_kwargs(decision_kind="save_event", event_db_id=None))


def test_llm_decision_save_event_rejects_error_type_set() -> None:
    """`save_event` with `error_type` set — the two states are mutually exclusive."""
    with pytest.raises(ValidationError, match="error_type=None"):
        LLMDecision(
            **_valid_kwargs(decision_kind="save_event", event_db_id=42, error_type="something")
        )


@pytest.mark.parametrize("kind", ["needs_clarification", "error"])
def test_llm_decision_failure_kinds_accept_valid_shape(kind: str) -> None:
    """Both failure kinds require `error_type` set and `event_db_id=None`."""
    decision = LLMDecision(
        **_valid_kwargs(decision_kind=kind, event_db_id=None, error_type="missing_date")
    )
    assert decision.decision_kind == kind
    assert decision.error_type == "missing_date"


@pytest.mark.parametrize("kind", ["needs_clarification", "error"])
def test_llm_decision_failure_kinds_reject_missing_error_type(kind: str) -> None:
    with pytest.raises(ValidationError, match="requires error_type"):
        LLMDecision(**_valid_kwargs(decision_kind=kind, event_db_id=None, error_type=None))


@pytest.mark.parametrize("kind", ["needs_clarification", "error"])
def test_llm_decision_failure_kinds_reject_event_db_id_set(kind: str) -> None:
    """A failure kind cannot also point at a persisted Event — mutually exclusive."""
    with pytest.raises(ValidationError, match="event_db_id=None"):
        LLMDecision(**_valid_kwargs(decision_kind=kind, event_db_id=42, error_type="missing_date"))


def test_llm_decision_answered_accepts_valid_shape() -> None:
    """`answered` requires both `event_db_id` and `error_type` to be `None`."""
    decision = LLMDecision(
        **_valid_kwargs(decision_kind="answered", event_db_id=None, error_type=None)
    )
    assert decision.decision_kind == "answered"
    assert decision.event_db_id is None
    assert decision.error_type is None


def test_llm_decision_answered_rejects_event_db_id_set() -> None:
    with pytest.raises(ValidationError, match="event_db_id=None"):
        LLMDecision(**_valid_kwargs(decision_kind="answered", event_db_id=42))


def test_llm_decision_answered_rejects_error_type_set() -> None:
    with pytest.raises(ValidationError, match="error_type=None"):
        LLMDecision(**_valid_kwargs(decision_kind="answered", error_type="oops"))


def test_llm_decision_rejects_unknown_decision_kind() -> None:
    with pytest.raises(ValidationError):
        LLMDecision(**_valid_kwargs(decision_kind="not-a-branch"))


def test_llm_decision_rationale_rejects_control_chars() -> None:
    """The regex re-check is defense-in-depth against a caller that skipped the sanitizer."""
    with pytest.raises(ValidationError, match="rationale must be sanitized"):
        LLMDecision(**_valid_kwargs(rationale="raw\nnewline"))


def test_llm_decision_rationale_field_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        LLMDecision(**_valid_kwargs(rationale="x" * (RATIONALE_CAP + 1)))


def test_llm_decision_rationale_survives_helper_then_boundary() -> None:
    """`format_stored_text` output round-trips through `LLMDecision` cleanly."""
    poisoned = "the LLM said:\nsomething\twith\x00 controls"
    sanitized = format_stored_text(poisoned, RATIONALE_CAP)
    decision = LLMDecision(**_valid_kwargs(rationale=sanitized))
    assert decision.rationale == sanitized
    assert "\n" not in decision.rationale
    assert "\t" not in decision.rationale
    assert "\x00" not in decision.rationale


def test_llm_decision_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        LLMDecision(**_valid_kwargs(unexpected="oops"))


# ---- migration 004 + repository ---------------------------------------------


def test_migration_004_lands_llm_decisions_table(isolated_db: sqlite3.Connection) -> None:
    """`user_version` reaches 4 and the table + both indexes exist."""
    version = int(isolated_db.execute("PRAGMA user_version").fetchone()[0])
    assert version >= 4

    tables = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "llm_decisions" in tables

    indexes = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_llm_decisions_run" in indexes
    assert "idx_llm_decisions_kind" in indexes


def test_migration_004_llm_decisions_table_shape(isolated_db: sqlite3.Connection) -> None:
    """`PRAGMA table_info` — all seven columns land with the expected shape."""
    columns = {
        row["name"]: row
        for row in isolated_db.execute("PRAGMA table_info(llm_decisions)").fetchall()
    }
    assert set(columns) == {
        "id",
        "run_id",
        "decision_kind",
        "event_db_id",
        "error_type",
        "rationale",
        "recorded_at",
    }
    # NOT NULL on run_id, decision_kind, rationale, recorded_at.
    assert columns["run_id"]["notnull"] == 1
    assert columns["decision_kind"]["notnull"] == 1
    assert columns["rationale"]["notnull"] == 1
    assert columns["recorded_at"]["notnull"] == 1
    assert columns["event_db_id"]["notnull"] == 0
    assert columns["error_type"]["notnull"] == 0


def test_record_and_query_roundtrip(isolated_db: sqlite3.Connection) -> None:
    """One-in-one-out — every field survives the DB roundtrip via Pydantic."""
    now = datetime(2026, 7, 28, 12, 5, tzinfo=UTC)
    decision = LLMDecision(
        run_id="parent-run",
        decision_kind="needs_clarification",
        event_db_id=None,
        error_type="missing_date",
        rationale="post says 'this friday' with no month/day",
        recorded_at=now,
    )
    row_id = record_llm_decision(isolated_db, decision)
    assert row_id > 0

    rows = query_llm_decisions(isolated_db, run_id="parent-run")
    assert len(rows) == 1
    restored = rows[0]
    assert restored.decision_kind == "needs_clarification"
    assert restored.error_type == "missing_date"
    assert restored.rationale == "post says 'this friday' with no month/day"
    assert restored.recorded_at == now


def test_query_filters_by_decision_kind(isolated_db: sqlite3.Connection) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    record_llm_decision(
        isolated_db,
        LLMDecision(
            run_id="parent-run",
            decision_kind="answered",
            rationale="ok",
            recorded_at=now,
        ),
    )
    record_llm_decision(
        isolated_db,
        LLMDecision(
            run_id="parent-run",
            decision_kind="error",
            error_type="rate_limited",
            rationale="rate limited",
            recorded_at=now + timedelta(seconds=1),
        ),
    )

    answered = query_llm_decisions(isolated_db, decision_kind="answered")
    errors = query_llm_decisions(isolated_db, decision_kind="error")

    assert [r.decision_kind for r in answered] == ["answered"]
    assert [r.decision_kind for r in errors] == ["error"]


def test_query_rejects_zero_limit(isolated_db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        query_llm_decisions(isolated_db, limit=0)


def test_query_uses_idx_llm_decisions_run_when_filtering_by_run_id(
    isolated_db: sqlite3.Connection,
) -> None:
    """`EXPLAIN QUERY PLAN` for `WHERE run_id = ?` names the index."""
    plan = isolated_db.execute(
        "EXPLAIN QUERY PLAN"
        " SELECT id FROM llm_decisions WHERE run_id = ?"
        " ORDER BY recorded_at DESC LIMIT 100",
        ("parent-run",),
    ).fetchall()
    plan_text = " ".join(str(row["detail"]) for row in plan)
    assert "idx_llm_decisions_run" in plan_text, (
        f"query plan did not use idx_llm_decisions_run: {plan_text!r}"
    )


def test_decision_kind_check_constraint_rejects_invalid_value(
    isolated_db: sqlite3.Connection,
) -> None:
    """The DB CHECK is the defense-in-depth lock for callers who bypass Pydantic."""
    now_iso = datetime(2026, 7, 28, tzinfo=UTC).isoformat()
    with pytest.raises(sqlite3.IntegrityError):
        isolated_db.execute(
            "INSERT INTO llm_decisions"
            " (run_id, decision_kind, event_db_id, error_type, rationale, recorded_at)"
            " VALUES ('parent-run', 'unknown-kind', NULL, NULL, 'r', ?)",
            (now_iso,),
        )


def test_run_id_foreign_key_rejects_orphan(isolated_db: sqlite3.Connection) -> None:
    """A `run_id` with no `agent_runs` row raises `IntegrityError` (FK enforced)."""
    with pytest.raises(sqlite3.IntegrityError):
        record_llm_decision(
            isolated_db,
            LLMDecision(
                run_id="no-such-run",
                decision_kind="answered",
                rationale="ok",
                recorded_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
            ),
        )


def test_query_skips_corrupt_row_with_warning_and_returns_healthy_rows(
    isolated_db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A row that fails Pydantic re-validation is skipped, not fatal."""
    now = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    record_llm_decision(
        isolated_db,
        LLMDecision(
            run_id="parent-run",
            decision_kind="answered",
            rationale="healthy",
            recorded_at=now,
        ),
    )
    now_iso = now.isoformat()
    isolated_db.execute(
        "INSERT INTO llm_decisions"
        " (run_id, decision_kind, event_db_id, error_type, rationale, recorded_at)"
        " VALUES ('parent-run', 'answered', NULL, NULL, 'raw\nnewline', ?)",
        (now_iso,),
    )
    isolated_db.commit()

    caplog.set_level(logging.WARNING, logger="planazo.observability.repository")

    rows = query_llm_decisions(isolated_db, run_id="parent-run")

    # Only the healthy row comes back.
    assert [r.rationale for r in rows] == ["healthy"]

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.repository" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "parent-run" in warnings[0].getMessage()


# ---- End-to-end wiring ------------------------------------------------------


def test_extract_once_writes_llm_decisions_for_save_event_plus_report(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed run: one `save_event` + one `report_extraction_status` → two rows.

    The extractor's LLM issues one successful `save_event` (which
    persists an `Event`) followed by one `report_extraction_status`
    (`needs_clarification`) — the shape "one event landed but the LLM
    also flagged something ambiguous". The `llm_decisions` table gets
    one row of each `decision_kind`.
    """
    user_id = _seed_user()
    source = _build_source()

    fake_call = MagicMock(
        side_effect=[
            _turn(
                "",
                [
                    {
                        "name": "save_event",
                        "arguments": {
                            "title": "Techno at Sala Apolo",
                            "category": "music",
                            "source": "instagram",
                            "source_url": _TEST_URL,
                            "start_utc": "2026-08-01T22:00:00+00:00",
                            "end_utc": "2026-08-02T04:00:00+00:00",
                            "city": "Barcelona",
                            "confidence": 0.9,
                            "event_index_in_post": 0,
                        },
                        "call_id": "call-s",
                    },
                    {
                        "name": "report_extraction_status",
                        "arguments": {
                            "status": "needs_clarification",
                            "error_type": "multiple_events_in_post",
                            "notes": "also a warmup event mentioned but details unclear",
                        },
                        "call_id": "call-r",
                    },
                ],
            ),
            _turn(""),
        ]
    )
    monkeypatch.setattr("planazo.agents.loop.call", fake_call)

    extract_once(_TEST_URL, delegator_user_id=user_id, source=source, model=STRONG)

    conn = db.connect()
    try:
        runs = query_agent_runs(conn, user_id=user_id)
        assert len(runs) == 1
        run_id = runs[0].run_id
        decisions = query_llm_decisions(conn, run_id=run_id)
    finally:
        conn.close()

    # Two rows tied to the same run_id, matching the LLM's two terminal-ish calls.
    assert len(decisions) == 2
    by_kind = {d.decision_kind: d for d in decisions}
    assert set(by_kind) == {"save_event", "needs_clarification"}
    saved_decision = by_kind["save_event"]
    assert saved_decision.event_db_id is not None
    assert saved_decision.event_db_id > 0
    assert saved_decision.error_type is None
    assert "Techno at Sala Apolo" in saved_decision.rationale

    clarification_decision = by_kind["needs_clarification"]
    assert clarification_decision.event_db_id is None
    assert clarification_decision.error_type == "multiple_events_in_post"
    assert "warmup" in clarification_decision.rationale


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("tech",),
    )


def test_run_once_writes_one_answered_llm_decision(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Recommender loop that answers on the first turn → one `answered` row.

    Post-M4-rebase: `run_once(user_id, intent, ...)` requires a typed
    `SearchIntent`, not raw user text. The observability write path
    is unchanged — one `llm_decisions` row per loop terminal.
    """
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    monkeypatch.setattr(
        event_agent,
        "run_loop",
        MagicMock(
            return_value=LoopResult(
                answer="here are three techno events", steps=1, stopped="answered"
            )
        ),
    )

    event_agent.run_once(user_id, _intent())

    conn = db.connect()
    try:
        runs = query_agent_runs(conn, user_id=user_id)
        assert len(runs) == 1
        run_id = runs[0].run_id
        decisions = query_llm_decisions(conn, run_id=run_id)
    finally:
        conn.close()

    assert len(decisions) == 1
    row = decisions[0]
    assert row.decision_kind == "answered"
    assert row.event_db_id is None
    assert row.error_type is None
    assert row.rationale == "here are three techno events"


def test_run_once_record_runs_false_disables_llm_decisions_writer(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_runs=False` disables the LLM-decisions writer alongside the JSONL one."""
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    monkeypatch.setattr(
        event_agent,
        "run_loop",
        MagicMock(return_value=LoopResult(answer="done", steps=1, stopped="answered")),
    )

    event_agent.run_once(user_id, _intent(), record_runs=False)

    conn = db.connect()
    try:
        decisions = query_llm_decisions(conn)
    finally:
        conn.close()
    assert decisions == []


def test_extract_once_writer_failure_does_not_propagate(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raise inside `record_llm_decision` is swallowed by the writer.

    Rule 4 hook: audit failures never break the primary flow. We
    monkeypatch `record_llm_decision` on the logging module (that is
    where `LLMDecisionLogger` looks it up) to raise
    `sqlite3.OperationalError`. The extractor still returns a shaped
    `ExtractionResult` and one WARNING lands on the logger.
    """
    user_id = _seed_user()
    source = _build_source()

    def _raising_record(*_args: object, **_kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated: writer failure")

    monkeypatch.setattr("planazo.observability.logging.record_llm_decision", _raising_record)

    fake_call = MagicMock(
        side_effect=[
            _turn(
                "",
                [
                    {
                        "name": "report_extraction_status",
                        "arguments": {
                            "status": "error",
                            "error_type": "low_confidence_extraction",
                            "notes": "",
                        },
                        "call_id": "call-r",
                    }
                ],
            ),
            _turn(""),
        ]
    )
    monkeypatch.setattr("planazo.agents.loop.call", fake_call)

    caplog.set_level(logging.WARNING, logger="planazo.observability.logging")

    # This call must complete without raising — the writer's exception is swallowed.
    result = extract_once(_TEST_URL, delegator_user_id=user_id, source=source, model=STRONG)
    assert result.status == "error"

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.logging" and rec.levelno == logging.WARNING
    ]
    assert warnings, "expected at least one WARNING from llm_decision_logger"
    assert any("llm_decision_logger write failed" in rec.getMessage() for rec in warnings)

    conn = db.connect()
    try:
        # The `agent_runs` row still landed (T3 writer is independent).
        runs = query_agent_runs(conn, user_id=user_id)
        assert len(runs) == 1
        # No `llm_decisions` rows landed — the writer raised.
        decisions = query_llm_decisions(conn)
    finally:
        conn.close()
    assert decisions == []
