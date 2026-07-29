"""Retention sweep + `--rotate-archived` CLI mode (curator FU1).

Two layers:

- Repository primitives (`list_purgeable_archived_events`,
  `purge_archived_events_older_than`) — SQL-only, no LLM.
- `curator.retention.run_retention` composition root + the CLI
  dispatch to `--rotate-archived DAYS`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from planazo.catalog import Event, get_event_by_id, insert_event
from planazo.catalog.repository import (
    list_purgeable_archived_events,
    purge_archived_events_older_than,
    soft_delete_event,
)
from planazo.curator.retention import (
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    run_retention,
)
from planazo.storage import db


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "source": "seed",
        "source_url": "https://seed/e/1",
        "title": "Meetup",
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


# ---------------------------------------------------------------------------
# Repository primitives
# ---------------------------------------------------------------------------


def test_purge_archived_events_older_than_deletes_only_old_archived(
    tmp_db: sqlite3.Connection,
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    old = insert_event(tmp_db, make_event(source_url="https://seed/old"))
    recent = insert_event(tmp_db, make_event(source_url="https://seed/recent"))
    live = insert_event(tmp_db, make_event(source_url="https://seed/live"))

    # Old archived 60 days ago.
    soft_delete_event(tmp_db, old, now=now - timedelta(days=60))
    # Recent archived yesterday.
    soft_delete_event(tmp_db, recent, now=now - timedelta(days=1))
    # `live` stays live.

    deleted = purge_archived_events_older_than(tmp_db, cutoff=now - timedelta(days=30))

    assert deleted == 1
    # Confirm exactly the old one is gone.
    assert get_event_by_id(tmp_db, old, include_archived=True) is None
    assert get_event_by_id(tmp_db, recent, include_archived=True) is not None
    assert get_event_by_id(tmp_db, live) is not None


def test_purge_returns_zero_when_no_archived_older_than_cutoff(
    tmp_db: sqlite3.Connection,
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    event_id = insert_event(tmp_db, make_event())
    soft_delete_event(tmp_db, event_id, now=now - timedelta(days=5))

    deleted = purge_archived_events_older_than(tmp_db, cutoff=now - timedelta(days=30))

    assert deleted == 0
    # Row still there (archived).
    assert get_event_by_id(tmp_db, event_id, include_archived=True) is not None


def test_purge_never_touches_a_live_row(tmp_db: sqlite3.Connection) -> None:
    live_id = insert_event(tmp_db, make_event())

    # Try to purge with a cutoff far in the future — even so, live rows are safe.
    deleted = purge_archived_events_older_than(tmp_db, cutoff=datetime(2099, 1, 1, tzinfo=UTC))

    assert deleted == 0
    assert get_event_by_id(tmp_db, live_id) is not None


def test_list_purgeable_returns_only_old_archived_ordered_oldest_first(
    tmp_db: sqlite3.Connection,
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    oldest = insert_event(tmp_db, make_event(source_url="https://seed/oldest"))
    middle = insert_event(tmp_db, make_event(source_url="https://seed/middle"))
    recent = insert_event(tmp_db, make_event(source_url="https://seed/recent"))
    live = insert_event(tmp_db, make_event(source_url="https://seed/live"))

    soft_delete_event(tmp_db, oldest, now=now - timedelta(days=90))
    soft_delete_event(tmp_db, middle, now=now - timedelta(days=60))
    soft_delete_event(tmp_db, recent, now=now - timedelta(days=1))

    purgeable = list_purgeable_archived_events(tmp_db, cutoff=now - timedelta(days=30))

    ids = [event.id for event in purgeable]
    assert ids == [oldest, middle]
    assert live not in ids


# ---------------------------------------------------------------------------
# run_retention composition root
# ---------------------------------------------------------------------------


def _fixed_now(when: datetime) -> Any:
    return lambda: when


def test_run_retention_deletes_when_dry_run_is_false(
    tmp_db: sqlite3.Connection, tmp_path: Path
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    old = insert_event(tmp_db, make_event(source_url="https://seed/old"))
    soft_delete_event(tmp_db, old, now=now - timedelta(days=60))
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_retention(
        retention_days=30,
        dry_run=False,
        audit_log_path=audit_log,
        now=_fixed_now(now),
    )

    assert result.deleted == 1
    assert result.dry_run is False
    assert result.retention_days == 30
    assert result.cutoff == now - timedelta(days=30)
    # Preview is filled from what would be deleted (already gone by return, but
    # the pre-DELETE read populated it).
    assert len(result.preview) == 1
    assert result.preview[0].id == old
    # Audit log line landed.
    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["events_archived"] == 1
    assert parsed["dry_run"] is False


def test_run_retention_dry_run_does_not_delete(tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    old = insert_event(tmp_db, make_event(source_url="https://seed/old"))
    soft_delete_event(tmp_db, old, now=now - timedelta(days=60))
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_retention(
        retention_days=30,
        dry_run=True,
        audit_log_path=audit_log,
        now=_fixed_now(now),
    )

    assert result.deleted == 0
    assert result.dry_run is True
    assert len(result.preview) == 1
    # The row is still there.
    assert get_event_by_id(tmp_db, old, include_archived=True) is not None
    # Audit log records the dry-run counters.
    parsed = json.loads(audit_log.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["events_archived"] == 0
    assert parsed["dry_run"] is True


def test_run_retention_rejects_out_of_range_days(tmp_path: Path) -> None:
    audit_log = tmp_path / "curator_runs.jsonl"

    with pytest.raises(ValueError, match="retention_days must be in"):
        run_retention(retention_days=0, audit_log_path=audit_log)
    with pytest.raises(ValueError, match="retention_days must be in"):
        run_retention(retention_days=MAX_RETENTION_DAYS + 1, audit_log_path=audit_log)


def test_run_retention_accepts_boundary_values(tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
    audit_log = tmp_path / "curator_runs.jsonl"

    # Both boundaries succeed with an empty DB (nothing archived).
    low = run_retention(retention_days=MIN_RETENTION_DAYS, audit_log_path=audit_log)
    high = run_retention(retention_days=MAX_RETENTION_DAYS, audit_log_path=audit_log)

    assert low.deleted == 0
    assert high.deleted == 0


def test_run_retention_leaves_live_rows_alone(tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    live_id = insert_event(tmp_db, make_event(source_url="https://seed/live"))
    audit_log = tmp_path / "curator_runs.jsonl"

    result = run_retention(
        retention_days=1,
        dry_run=False,
        audit_log_path=audit_log,
        now=_fixed_now(now),
    )

    assert result.deleted == 0
    assert get_event_by_id(tmp_db, live_id) is not None


def test_run_retention_writes_audit_line_even_when_zero_deleted(
    tmp_db: sqlite3.Connection, tmp_path: Path
) -> None:
    audit_log = tmp_path / "curator_runs.jsonl"

    run_retention(retention_days=30, audit_log_path=audit_log)

    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
