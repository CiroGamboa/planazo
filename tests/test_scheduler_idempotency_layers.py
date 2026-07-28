"""Targeted unit tests for the two idempotency layers ADR 0011 §Consequences claims.

Layer 1: `events_exist_for_source_url` pre-check in `_process_source_url` —
keeps STRONG-tier LLM budget off already-persisted URLs before any
extractor call.

Layer 2: composite `UNIQUE(source_url, event_index_in_post)` in `events` —
the second-line defense if the pre-check races (concurrent tick, corrupted
`scan_state`, manual re-tick). Fires as `sqlite3.IntegrityError` on the
second insert against the same natural-key pair.

The plan's Rev-1 M4 fix rewrites what was originally a multi-thread race
test into two focused unit tests: each layer is exercised in isolation so
the guarantee ADR 0011 names is provably load-bearing on its own.
Threading is not used — SQLite's file lock already serialises writes, and
these tests target *what happens when the pre-check is bypassed*, not
*what happens under concurrent access*.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planazo.catalog.models import Event
from planazo.catalog.repository import insert_event
from planazo.extraction.models import ExtractionResult
from planazo.scheduler.repository import bootstrap_system_user
from planazo.scheduler.service import _process_source_url
from planazo.storage import db

_POST_URL = "https://www.instagram.com/p/AbCdEf/"


class _NeverCalledExtractor:
    """Test double that fails the test if the scheduler invokes it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, delegator_user_id: int) -> ExtractionResult:
        self.calls.append((url, delegator_user_id))
        raise AssertionError(
            "extractor was called despite the URL having existing events in the DB"
        )


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def _make_event(url: str = _POST_URL, index: int = 0) -> Event:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    return Event(
        source="instagram",
        source_url=url,
        title="Test event",
        start_utc=now,
        end_utc=now,
        category="music",
        city="Barcelona",
        confidence=0.9,
        event_index_in_post=index,
    )


def _fixed_now() -> datetime:
    return datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_pre_check_layer_prevents_extractor_call_when_events_exist(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Layer 1 — `events_exist_for_source_url` short-circuits before `extractor`.

    Pre-populate the events table with one row for the target URL, then
    run `_process_source_url` over the same URL with a booby-trapped
    extractor. The pre-check must fire and short-circuit; the extractor
    stub raises if it is called, so the assertion is by construction.
    """
    system_user = bootstrap_system_user(conn)
    assert system_user.id is not None
    audit_log = tmp_path / "runs.jsonl"

    # Layer 1 setup: the URL is already persisted in `events`.
    row_id = insert_event(conn, _make_event())
    assert row_id > 0

    trap = _NeverCalledExtractor()
    record = _process_source_url(
        conn=conn,
        source_url=_POST_URL,
        source_kind="post",
        cadence=timedelta(hours=6),  # ignored when the pre-check short-circuits
        backend_client=None,
        backend_name=None,
        extractor=trap,
        now=_fixed_now,
        audit_log_path=audit_log,
        system_user_id=system_user.id,
        bypass_cadence_gate=False,
    )

    assert trap.calls == []
    assert record.posts_skipped_idempotent == 1
    assert record.posts_extracted_ok == 0
    assert record.posts_extracted_error == 0

    # And no new row landed — the pre-check kept the counter at 1.
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source_url = ?", (_POST_URL,)
    ).fetchone()[0]
    assert count == 1


def test_UNIQUE_constraint_layer_catches_double_write_when_pre_check_missed(  # noqa: N802 - plan-mandated name; UNIQUE is a SQL keyword
    conn: sqlite3.Connection,
) -> None:
    """Layer 2 — the composite UNIQUE closes the race the pre-check leaves open.

    Bypass the pre-check by calling `insert_event` directly twice with the
    same `(source_url, event_index_in_post)` pair. The first insert lands;
    the second raises `sqlite3.IntegrityError`. This is the guarantee ADR
    0011 §Consequences claims — the two-layer defense is not just the
    pre-check plus a "hope no one races us" — the schema itself refuses
    the second write.
    """
    first_id = insert_event(conn, _make_event(index=0))
    assert first_id > 0

    with pytest.raises(sqlite3.IntegrityError):
        insert_event(conn, _make_event(index=0))

    # The first row is intact; the failed second insert added nothing.
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source_url = ?", (_POST_URL,)
    ).fetchone()[0]
    assert count == 1


def test_UNIQUE_constraint_allows_multi_slot_inserts_for_same_url(  # noqa: N802 - matches sibling test convention; UNIQUE is a SQL keyword
    conn: sqlite3.Connection,
) -> None:
    """Layer 2 is per-slot — different `event_index_in_post` values coexist.

    Locks the ADR 0012 multi-event contract as it interacts with the
    scheduler's idempotency layers: a carousel that produces two events
    for one URL is NOT the failure mode the composite UNIQUE guards
    against; two independent runs producing the same slot IS.
    """
    slot_a = insert_event(conn, _make_event(index=0))
    slot_b = insert_event(conn, _make_event(index=1))
    assert slot_a != slot_b

    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source_url = ?", (_POST_URL,)
    ).fetchone()[0]
    assert count == 2
