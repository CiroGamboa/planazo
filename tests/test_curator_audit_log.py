"""Curator audit log — `CuratorRunRecord` shape + `append_run_record` behavior.

Mirrors `test_scheduler_audit_log.py` in shape: one record per tick,
compact JSON separators, parent directory created, line-per-record shape
that `tail -f` renders cleanly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.curator import CuratorRunRecord, append_run_record


def _record(**overrides: object) -> CuratorRunRecord:
    defaults: dict[str, object] = {
        "run_id": "curator-run-1",
        "started_at": datetime(2026, 7, 28, 3, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 7, 28, 3, 0, 30, tzinfo=UTC),
        "events_examined": 25,
        "events_archived": 3,
        "events_merged": 1,
        "categories_updated": 2,
        "errors": [],
        "dry_run": False,
    }
    defaults.update(overrides)
    return CuratorRunRecord(**defaults)  # type: ignore[arg-type]


def test_append_creates_parent_directory(tmp_path: Path) -> None:
    """Parent directory does not need to exist beforehand."""
    audit_path = tmp_path / "var" / "curator_runs.jsonl"

    append_run_record(_record(), audit_path)

    assert audit_path.parent.is_dir()
    assert audit_path.is_file()


def test_append_writes_one_json_line_per_record(tmp_path: Path) -> None:
    audit_path = tmp_path / "curator_runs.jsonl"

    append_run_record(_record(run_id="run-1"), audit_path)
    append_run_record(_record(run_id="run-2", events_archived=0), audit_path)

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["run_id"] == "run-1"
    assert second["run_id"] == "run-2"
    assert second["events_archived"] == 0


def test_append_uses_compact_separators(tmp_path: Path) -> None:
    """Compact JSON keeps `tail -f` output one-record-per-terminal-row."""
    audit_path = tmp_path / "curator_runs.jsonl"

    append_run_record(_record(), audit_path)

    written = audit_path.read_text(encoding="utf-8").splitlines()[0]
    assert '", "' not in written  # no space after key separator
    assert '":' in written  # bare colon separator


def test_record_serialises_datetimes_as_iso_8601(tmp_path: Path) -> None:
    audit_path = tmp_path / "curator_runs.jsonl"

    append_run_record(_record(), audit_path)

    parsed = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["started_at"] == "2026-07-28T03:00:00Z"
    assert parsed["ended_at"] == "2026-07-28T03:00:30Z"


def test_record_carries_errors_list_and_dry_run_flag(tmp_path: Path) -> None:
    audit_path = tmp_path / "curator_runs.jsonl"

    append_run_record(
        _record(errors=["not_found: id 999", "already_archived: id 5"], dry_run=True),
        audit_path,
    )

    parsed = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["errors"] == ["not_found: id 999", "already_archived: id 5"]
    assert parsed["dry_run"] is True


def test_record_rejects_negative_counters_at_pydantic_boundary() -> None:
    with pytest.raises(ValidationError):
        _record(events_archived=-1)
    with pytest.raises(ValidationError):
        _record(events_examined=-5)


def test_record_rejects_empty_run_id_at_pydantic_boundary() -> None:
    with pytest.raises(ValidationError):
        _record(run_id="")


def test_record_extra_field_forbidden_at_pydantic_boundary() -> None:
    """`extra="forbid"` catches typos before the row hits the log."""
    with pytest.raises(ValidationError):
        CuratorRunRecord(  # type: ignore[call-arg]
            run_id="r",
            started_at=datetime(2026, 7, 28, tzinfo=UTC),
            ended_at=datetime(2026, 7, 28, 0, 1, tzinfo=UTC),
            events_examined=0,
            events_archived=0,
            events_merged=0,
            categories_updated=0,
            typo_field="oops",
        )
