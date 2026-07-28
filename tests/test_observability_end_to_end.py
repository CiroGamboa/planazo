"""End-to-end wiring — `extract_once` + `event_agent.run_once` each write one row.

Locks the composition-root discipline: both loops attribute a completed
run through `AgentRunLogger` to the `agent_runs` table, using the same
`run_id` as the JSONL sidecar. Also proves the Recommender's
`record_runs=False` seam disables the SQLite writer alongside the JSONL
one — a caller that opts out of persisted audit gets neither.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentlib.core import CHEAP, STRONG, Result
from planazo.agents import event_agent
from planazo.agents.extractor import extract_once
from planazo.agents.loop import LoopResult
from planazo.identity import get_or_create_user
from planazo.memory import facts, rules
from planazo.observability import query_agent_runs
from planazo.query.models import SearchIntent
from planazo.sources.config import MediaTypeFlags, SourceConfig
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClientProtocol
from planazo.sources.instagram.model_view import InstaloaderPostView
from planazo.storage import db


def _intent() -> SearchIntent:
    """Minimal SearchIntent fixture — matches test_event_agent.py's `_intent()`."""
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("tech",),
    )


def _answered() -> LoopResult:
    return LoopResult(answer="done", steps=1, stopped="answered")


_TEST_URL = "https://www.instagram.com/p/ABC123/"


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


# ---- extract_once ----------------------------------------------------------


def test_extract_once_writes_one_agent_runs_row(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Extractor loop lands one `agent_runs` row per run, kind=extractor."""
    user_id = _seed_user()
    source = _build_source()

    fake_call = MagicMock(
        side_effect=[
            _turn(
                "",
                [
                    {
                        "name": "report_extraction_status",
                        "arguments": {
                            "status": "needs_clarification",
                            "error_type": "missing_date",
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

    extract_once(_TEST_URL, delegator_user_id=user_id, source=source, model=STRONG)

    conn = db.connect()
    try:
        rows = query_agent_runs(conn, user_id=user_id)
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_kind == "extractor"
    assert row.user_id == user_id
    assert row.stopped == "answered"
    assert row.user_query.startswith("Extract every distinct event")
    # The Extractor loop ran two turns (report_extraction_status → empty
    # answer) — `steps_count` ties the row to observable loop state.
    assert row.steps_count == 2


def test_extract_once_agent_runs_run_id_matches_extraction_log(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `agent_runs.run_id` matches the JSONL sidecar's `run_id`.

    Locks the composition-root discipline: one run has one id across
    every audit surface (JSONL trace, extraction-runs index, agent_runs
    row). A future refactor that generates a new UUID for the DB writer
    breaks this join.
    """
    user_id = _seed_user()
    source = _build_source()

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
                        "call_id": "c1",
                    }
                ],
            ),
            _turn(""),
        ]
    )
    monkeypatch.setattr("planazo.agents.loop.call", fake_call)

    extract_once(_TEST_URL, delegator_user_id=user_id, source=source, model=STRONG)

    log_path = isolated_stores / "extraction_runs.jsonl"
    log_lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    trace_run_id = log_lines[0]["run_id"]

    conn = db.connect()
    try:
        rows = query_agent_runs(conn, user_id=user_id)
    finally:
        conn.close()
    assert rows[0].run_id == trace_run_id


# ---- event_agent.run_once --------------------------------------------------


def test_run_once_writes_one_agent_runs_row(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Recommender loop lands one `agent_runs` row per run, kind=recommender.

    Post-M4-rebase: `run_once(user_id, intent, ...)` no longer receives the
    user's raw text (interpreter upstream turned it into `SearchIntent`).
    The observability write stores `intent.model_dump_json()` as
    `user_query` — the concrete input the recommender ran against.
    """
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    intent = _intent()
    monkeypatch.setattr(event_agent, "run_loop", MagicMock(return_value=_answered()))

    event_agent.run_once(user_id, intent)

    conn = db.connect()
    try:
        rows = query_agent_runs(conn, user_id=user_id)
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_kind == "recommender"
    assert row.user_id == user_id
    # `user_query` holds the JSON-serialized intent, sanitized via
    # `format_stored_text` (newlines collapsed to spaces — Pydantic's
    # `model_dump_json()` is single-line already, but the sanitizer
    # runs regardless).
    assert '"city":"Barcelona"' in row.user_query
    assert '"categories":["tech"]' in row.user_query
    assert row.final_answer == "done"
    assert row.stopped == "answered"
    # `_answered()` returned steps=1 — `steps_count == 1` locks the row
    # to observable loop state and would trip if the loop invocation drifts.
    assert row.steps_count == 1


def test_run_once_record_runs_false_disables_sqlite_writer(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_runs=False` disables both JSONL and SQLite audit writers.

    The existing seam already gated the JSONL writer; T3 extends it to
    the `agent_runs` row so a caller that opts out of persisted audit
    gets neither surface.
    """
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    monkeypatch.setattr(event_agent, "run_loop", MagicMock(return_value=_answered()))

    event_agent.run_once(user_id, _intent(), record_runs=False)

    conn = db.connect()
    try:
        rows = query_agent_runs(conn, user_id=user_id)
    finally:
        conn.close()
    assert rows == []


# NOTE: `test_run_once_records_null_user_id_when_no_identity_supplied` was
# removed post-rebase. The M4-era `run_once(user_id, intent, ...)` requires
# `user_id: int` positionally, so the "no identity supplied" branch no
# longer exists at the run_once seam. If the operator-run-without-identity
# pattern comes back (e.g., a `--anonymous` CLI mode), a new test lands
# alongside it.
