"""Soft-delete primitives on the catalog repository (curator T1 / ADR 0020).

Exercises `soft_delete_event`, `restore_event`, `update_event_category`, and
the archive-filtering behavior of `query_events` + `get_event_by_id`. The
default read surface never sees archived rows; `include_archived=True` is
the admin escape hatch the curator's own tools rely on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from planazo.catalog import (
    Event,
    get_event_by_id,
    insert_event,
    query_events,
)
from planazo.catalog.repository import (
    restore_event,
    soft_delete_event,
    update_event_category,
)
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


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_migration_008_adds_archived_at_column(conn: sqlite3.Connection) -> None:
    """`archived_at TEXT` exists on `events` after migration 008."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "archived_at" in columns


def test_fresh_insert_reads_back_with_archived_at_none(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())

    row = get_event_by_id(conn, event_id)

    assert row is not None
    assert row.archived_at is None


def test_soft_delete_event_stamps_archived_at_and_returns_true(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())
    stamped = datetime(2026, 12, 1, tzinfo=UTC)

    outcome = soft_delete_event(conn, event_id, now=stamped)

    assert outcome is True
    row = get_event_by_id(conn, event_id, include_archived=True)
    assert row is not None
    assert row.archived_at == stamped


def test_soft_delete_event_defaults_now_to_current_utc(conn: sqlite3.Connection) -> None:
    """Called without `now`, the timestamp is close to `datetime.now(UTC)`."""
    event_id = insert_event(conn, make_event())
    before = datetime.now(UTC)

    soft_delete_event(conn, event_id)

    row = get_event_by_id(conn, event_id, include_archived=True)
    assert row is not None
    assert row.archived_at is not None
    assert (row.archived_at - before).total_seconds() < 5


def test_soft_delete_event_returns_false_when_id_missing(conn: sqlite3.Connection) -> None:
    assert soft_delete_event(conn, 999_999) is False


def test_soft_delete_event_returns_false_when_already_archived(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())
    soft_delete_event(conn, event_id)

    assert soft_delete_event(conn, event_id) is False


def test_restore_event_nulls_archived_at_and_returns_true(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())
    soft_delete_event(conn, event_id)

    outcome = restore_event(conn, event_id)

    assert outcome is True
    row = get_event_by_id(conn, event_id)
    assert row is not None
    assert row.archived_at is None


def test_restore_event_returns_false_when_row_is_already_live(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())

    assert restore_event(conn, event_id) is False


def test_restore_event_returns_false_when_id_missing(conn: sqlite3.Connection) -> None:
    assert restore_event(conn, 999_999) is False


def test_query_events_hides_archived_rows_by_default(conn: sqlite3.Connection) -> None:
    live_id = insert_event(conn, make_event(source_url="https://seed/live"))
    archived_id = insert_event(conn, make_event(source_url="https://seed/archived"))
    soft_delete_event(conn, archived_id)

    found = query_events(conn)

    assert [event.id for event in found] == [live_id]


def test_query_events_include_archived_returns_both(conn: sqlite3.Connection) -> None:
    live_id = insert_event(conn, make_event(source_url="https://seed/live"))
    archived_id = insert_event(conn, make_event(source_url="https://seed/archived"))
    soft_delete_event(conn, archived_id)

    found = query_events(conn, include_archived=True)

    assert {event.id for event in found} == {live_id, archived_id}


def test_get_event_by_id_hides_archived_by_default(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())
    soft_delete_event(conn, event_id)

    assert get_event_by_id(conn, event_id) is None


def test_get_event_by_id_include_archived_returns_the_row(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event())
    soft_delete_event(conn, event_id)

    row = get_event_by_id(conn, event_id, include_archived=True)

    assert row is not None
    assert row.id == event_id
    assert row.archived_at is not None


def test_update_event_category_changes_category_on_live_row(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event(category="tech"))

    outcome = update_event_category(conn, event_id, "cultural")

    assert outcome is True
    row = get_event_by_id(conn, event_id)
    assert row is not None
    assert row.category == "cultural"


def test_update_event_category_refuses_archived_row(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event(category="tech"))
    soft_delete_event(conn, event_id)

    outcome = update_event_category(conn, event_id, "cultural")

    assert outcome is False
    row = get_event_by_id(conn, event_id, include_archived=True)
    assert row is not None
    assert row.category == "tech"


def test_update_event_category_returns_false_when_id_missing(conn: sqlite3.Connection) -> None:
    assert update_event_category(conn, 999_999, "cultural") is False
