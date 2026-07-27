import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from planazo import identity
from planazo.schemas.domain import Event, ExtractionRunIndexEntry
from planazo.storage import dao, db
from tools.schema import schema_for


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
    """One shared `:memory:` connection for the primitive tier.

    A fresh `sqlite3.connect(":memory:")` per dao call would hand each call its
    own empty database, so every round-trip assertion would read back nothing.
    """
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def db_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real file for `save_event`/`search_events`.

    Those two open and close their own connection per call, so they need a file
    to share state across calls; `:memory:` would give each call an empty
    database.
    """
    path = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


# --------------------------------------------------------------------------
# Primitives.
# --------------------------------------------------------------------------


def test_insert_event_round_trips_through_query_events(conn: sqlite3.Connection) -> None:
    event_id = dao.insert_event(conn, make_event(extra={"organizer": "BCN AI"}))

    found = dao.query_events(conn)

    assert len(found) == 1
    assert found[0].id == event_id
    assert found[0].source_url == "https://meetup.example/e/1"
    assert found[0].start_utc == datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    assert found[0].extra == {"organizer": "BCN AI"}
    assert found[0].price_cents == 0
    assert found[0].ingested_at is not None


def test_query_events_applies_every_supplied_filter(conn: sqlite3.Connection) -> None:
    dao.insert_event(conn, make_event(source_url="https://e/1", category="tech"))
    dao.insert_event(conn, make_event(source_url="https://e/2", category="music"))
    dao.insert_event(conn, make_event(source_url="https://e/3", city="Madrid"))
    dao.insert_event(
        conn,
        make_event(
            source_url="https://e/4",
            start_utc=datetime(2026, 9, 1, 19, 0, tzinfo=UTC),
            end_utc=datetime(2026, 9, 1, 21, 0, tzinfo=UTC),
        ),
    )

    assert {e.source_url for e in dao.query_events(conn, category="tech")} == {
        "https://e/1",
        "https://e/3",
        "https://e/4",
    }
    assert {e.source_url for e in dao.query_events(conn, city="Madrid")} == {"https://e/3"}
    assert {
        e.source_url for e in dao.query_events(conn, start_after=datetime(2026, 8, 15, tzinfo=UTC))
    } == {"https://e/4"}
    assert len(dao.query_events(conn, max_results=2)) == 2


def test_insert_event_rejects_a_duplicate_source_url(conn: sqlite3.Connection) -> None:
    dao.insert_event(conn, make_event())

    with pytest.raises(sqlite3.IntegrityError):
        dao.insert_event(conn, make_event(title="A Different Title"))


def test_record_extraction_run_round_trips_through_list_extraction_runs(
    conn: sqlite3.Connection,
) -> None:
    user = identity.get_or_create_user(conn, "tg-1", "Dani")
    assert user.id is not None

    entry_id = dao.record_extraction_run(
        conn,
        ExtractionRunIndexEntry(
            run_id="run-1", user_id=user.id, url="https://instagram.example/p/1"
        ),
    )

    stored = dao.list_extraction_runs(conn, user.id)
    assert len(stored) == 1
    assert stored[0].id == entry_id
    assert stored[0].run_id == "run-1"
    assert stored[0].url == "https://instagram.example/p/1"
    assert stored[0].started_at is not None


# --------------------------------------------------------------------------
# Tool-ready wrappers.
# --------------------------------------------------------------------------


def test_save_event_persists_and_returns_the_row_id(db_file: Path) -> None:
    result = dao.save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    assert isinstance(result["event_db_id"], int)
    saved = result["saved"]
    assert isinstance(saved, dict)
    assert saved["source_url"] == "https://meetup.example/e/1"
    assert saved["id"] == result["event_db_id"]
    assert db_file.exists()


def test_save_event_rejects_an_unparseable_start_utc(db_file: Path) -> None:
    result = dao.save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="not-a-date",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    assert result["error_type"] == "invalid_event_data"
    assert not db_file.exists()


def test_save_event_reports_a_duplicate_source_url_with_the_existing_row_id(
    db_file: Path,
) -> None:
    first = dao.save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    second = dao.save_event(
        title="AI Meetup (re-extracted, worse)",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.4,
    )

    assert second["error_type"] == "duplicate_event"
    assert second["event_db_id"] == first["event_db_id"]

    # The earlier row is untouched: no upsert, no second row.
    found = dao.search_events()
    assert found["total"] == 1
    events = found["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "AI Meetup"


def test_search_events_finds_a_row_saved_by_a_separate_call(db_file: Path) -> None:
    dao.save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    # Each wrapper opens its own connection, so finding the row here proves the
    # two calls share state through the file rather than an in-process cache.
    result = dao.search_events(category="tech", city="Barcelona")

    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["source_url"] == "https://meetup.example/e/1"

    assert dao.search_events(category="music")["total"] == 0


def test_search_events_rejects_an_unparseable_start_after(db_file: Path) -> None:
    result = dao.search_events(start_after="tonight")

    assert result["error_type"] == "invalid_search_filter"


@pytest.mark.parametrize("max_results", [-1, 0])
def test_search_events_rejects_a_non_positive_max_results(db_file: Path, max_results: int) -> None:
    for index in range(3):
        dao.save_event(
            title=f"AI Meetup {index}",
            category="tech",
            source="meetup",
            source_url=f"https://meetup.example/e/{index}",
            start_utc="2026-08-01T19:00:00+00:00",
            end_utc="2026-08-01T21:00:00+00:00",
            city="Barcelona",
            confidence=0.9,
        )
    # The rows are there, so the assertions below are about the cap and not
    # about an empty table. SQLite reads `LIMIT -1` as "no limit", which is how
    # a model-supplied cap of -1 used to page the entire table back.
    assert dao.search_events(max_results=2)["total"] == 2

    result = dao.search_events(max_results=max_results)

    assert result["error_type"] == "invalid_search_filter"
    assert "events" not in result


def test_the_wrapper_tool_schemas_expose_no_connection_parameter() -> None:
    for tool in (dao.save_event, dao.search_events):
        properties = schema_for(tool)["parameters"]["properties"]
        assert "conn" not in properties
        assert not [name for name in properties if "connection" in name.lower()]
