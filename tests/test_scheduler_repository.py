"""Unit tests for `planazo.scheduler.repository` — `scan_state` primitives."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from planazo.scheduler.models import ScanState
from planazo.scheduler.repository import (
    get_scan_state,
    upsert_scan_state,
)
from planazo.storage import db


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_get_scan_state_absent_returns_none(conn: sqlite3.Connection) -> None:
    assert get_scan_state(conn, "https://www.instagram.com/p/A/") is None


def test_upsert_scan_state_inserts_when_absent(conn: sqlite3.Connection) -> None:
    state = ScanState(
        source_url="https://www.instagram.com/p/A/",
        last_scanned_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        last_success_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        consecutive_failures=0,
    )
    upsert_scan_state(conn, state)

    reloaded = get_scan_state(conn, "https://www.instagram.com/p/A/")
    assert reloaded == state


def test_upsert_scan_state_updates_when_present(conn: sqlite3.Connection) -> None:
    first = ScanState(
        source_url="https://www.instagram.com/p/A/",
        last_scanned_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        last_success_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        consecutive_failures=0,
    )
    upsert_scan_state(conn, first)

    second = first.model_copy(
        update={
            "last_scanned_at": datetime(2026, 7, 28, 18, 0, tzinfo=UTC),
            "consecutive_failures": 2,
        }
    )
    upsert_scan_state(conn, second)

    reloaded = get_scan_state(conn, "https://www.instagram.com/p/A/")
    assert reloaded == second


def test_upsert_scan_state_null_timestamps_survive_roundtrip(conn: sqlite3.Connection) -> None:
    # A freshly-seeded row has never been scanned — both timestamps are None
    # and must persist as SQL NULL, then reload as `None`, not as an empty
    # string that would fail `datetime.fromisoformat`.
    state = ScanState(source_url="https://www.instagram.com/p/A/")
    upsert_scan_state(conn, state)

    reloaded = get_scan_state(conn, "https://www.instagram.com/p/A/")
    assert reloaded is not None
    assert reloaded.last_scanned_at is None
    assert reloaded.last_success_at is None


def test_scan_state_source_url_is_primary_key(conn: sqlite3.Connection) -> None:
    # A second raw INSERT for the same `source_url` collides with the PK.
    # `upsert_scan_state` uses ON CONFLICT to swap the value; a naive INSERT
    # (as a downstream consumer might attempt) raises `IntegrityError`.
    conn.execute(
        "INSERT INTO scan_state (source_url, last_scanned_at, last_success_at,"
        " consecutive_failures) VALUES (?, ?, ?, ?)",
        ("https://www.instagram.com/p/A/", None, None, 0),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scan_state (source_url, last_scanned_at, last_success_at,"
            " consecutive_failures) VALUES (?, ?, ?, ?)",
            ("https://www.instagram.com/p/A/", None, None, 0),
        )


def test_get_scan_state_returns_only_the_requested_url(conn: sqlite3.Connection) -> None:
    upsert_scan_state(conn, ScanState(source_url="https://www.instagram.com/p/A/"))
    upsert_scan_state(
        conn,
        ScanState(source_url="https://www.instagram.com/p/B/", consecutive_failures=3),
    )

    a = get_scan_state(conn, "https://www.instagram.com/p/A/")
    b = get_scan_state(conn, "https://www.instagram.com/p/B/")
    assert a is not None
    assert b is not None
    assert a.consecutive_failures == 0
    assert b.consecutive_failures == 3
