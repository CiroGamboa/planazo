import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from planazo import identity
from planazo.catalog import (
    Event,
    ExtractionRunIndexEntry,
    insert_event,
    list_extraction_runs,
    query_events,
    record_extraction_run,
)
from planazo.storage import db


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "source": "meetup",
        "source_url": "https://meetup.example/e/1",
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
    """One shared `:memory:` connection for the primitive tier."""
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_insert_event_round_trips_through_query_events(conn: sqlite3.Connection) -> None:
    event_id = insert_event(conn, make_event(extra={"organizer": "BCN AI"}))

    found = query_events(conn)

    assert len(found) == 1
    assert found[0].id == event_id
    assert found[0].source_url == "https://meetup.example/e/1"
    assert found[0].start_utc == datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    assert found[0].extra == {"organizer": "BCN AI"}
    assert found[0].price_cents == 0
    assert found[0].ingested_at is not None


def test_query_events_applies_every_supplied_filter(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event(source_url="https://e/1", category="tech"))
    insert_event(conn, make_event(source_url="https://e/2", category="music"))
    insert_event(conn, make_event(source_url="https://e/3", city="Madrid"))
    insert_event(
        conn,
        make_event(
            source_url="https://e/4",
            start_utc=datetime(2026, 9, 1, 19, 0, tzinfo=UTC),
            end_utc=datetime(2026, 9, 1, 21, 0, tzinfo=UTC),
        ),
    )

    assert {e.source_url for e in query_events(conn, category="tech")} == {
        "https://e/1",
        "https://e/3",
        "https://e/4",
    }
    assert {e.source_url for e in query_events(conn, city="Madrid")} == {"https://e/3"}
    assert {
        e.source_url for e in query_events(conn, start_after=datetime(2026, 8, 15, tzinfo=UTC))
    } == {"https://e/4"}
    assert len(query_events(conn, max_results=2)) == 2


def test_insert_event_rejects_a_duplicate_source_url(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event())

    with pytest.raises(sqlite3.IntegrityError):
        insert_event(conn, make_event(title="A Different Title"))


def test_record_extraction_run_round_trips_through_list_extraction_runs(
    conn: sqlite3.Connection,
) -> None:
    user = identity.get_or_create_user(conn, "tg-1", "Dani")
    assert user.id is not None

    entry_id = record_extraction_run(
        conn,
        ExtractionRunIndexEntry(
            run_id="run-1", user_id=user.id, url="https://instagram.example/p/1"
        ),
    )

    stored = list_extraction_runs(conn, user.id)
    assert len(stored) == 1
    assert stored[0].id == entry_id
    assert stored[0].run_id == "run-1"
    assert stored[0].url == "https://instagram.example/p/1"
    assert stored[0].started_at is not None
