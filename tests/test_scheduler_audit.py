"""Unit tests for `planazo.scheduler.audit` — the JSONL run-record writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from planazo.scheduler.audit import append_run_record
from planazo.scheduler.models import SchedulerRunRecord


def _make_record(
    run_id: str = "run-1",
    *,
    errors: list[str] | None = None,
) -> SchedulerRunRecord:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    return SchedulerRunRecord(
        run_id=run_id,
        source_url="https://www.instagram.com/p/A/",
        source_kind="post",
        backend=None,
        gate_reason="first_run",
        posts_discovered=0,
        posts_extracted_ok=1,
        posts_extracted_error=0,
        posts_skipped_idempotent=0,
        errors=errors or [],
        started_at=now,
        ended_at=now + timedelta(seconds=2),
    )


def test_append_run_record_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "deeper" / "still-deeper" / "scheduler_runs.jsonl"
    append_run_record(_make_record(), target)
    assert target.exists()
    assert target.parent.is_dir()


def test_append_run_record_writes_one_line_per_call(tmp_path: Path) -> None:
    target = tmp_path / "scheduler_runs.jsonl"
    append_run_record(_make_record("run-1"), target)
    append_run_record(_make_record("run-2"), target)
    append_run_record(_make_record("run-3"), target)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["run_id"] for line in lines] == ["run-1", "run-2", "run-3"]


def test_append_run_record_json_roundtrips_through_pydantic(tmp_path: Path) -> None:
    target = tmp_path / "scheduler_runs.jsonl"
    original = _make_record(errors=["rate_limited: hikerapi 429"])
    append_run_record(original, target)

    line = target.read_text(encoding="utf-8").strip()
    restored = SchedulerRunRecord.model_validate_json(line)
    assert restored == original


def test_append_run_record_uses_compact_separators(tmp_path: Path) -> None:
    # Compact JSON keeps each line short enough for `tail -f` to render one
    # record per terminal row. Matches monitor/logging.py.
    target = tmp_path / "scheduler_runs.jsonl"
    append_run_record(_make_record(), target)
    line = target.read_text(encoding="utf-8").strip()
    assert ", " not in line
    assert ": " not in line
