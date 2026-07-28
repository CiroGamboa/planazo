import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from planazo import identity
from planazo.catalog import (
    Event,
    ExtractionRunIndexEntry,
    events_exist_for_source_url,
    insert_event,
    list_extraction_runs,
    query_events,
    record_extraction_run,
)
from planazo.query.models import EventCategory
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


def test_insert_event_rejects_a_duplicate_source_url_and_slot(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event())

    with pytest.raises(sqlite3.IntegrityError):
        insert_event(conn, make_event(title="A Different Title"))


def test_insert_event_allows_two_slots_on_the_same_source_url(conn: sqlite3.Connection) -> None:
    first_id = insert_event(conn, make_event(event_index_in_post=0))
    second_id = insert_event(conn, make_event(title="Second night", event_index_in_post=1))

    assert first_id != second_id
    found = query_events(conn)
    assert {event.event_index_in_post for event in found} == {0, 1}


def test_events_exist_for_source_url_returns_empty_for_unknown_url(
    conn: sqlite3.Connection,
) -> None:
    assert events_exist_for_source_url(conn, "https://unknown/") == []


def test_events_exist_for_source_url_returns_the_default_slot_after_one_insert(
    conn: sqlite3.Connection,
) -> None:
    insert_event(conn, make_event())

    assert events_exist_for_source_url(conn, "https://meetup.example/e/1") == [0]


def test_events_exist_for_source_url_returns_every_slot_sorted_ascending(
    conn: sqlite3.Connection,
) -> None:
    # Insert out of order to prove the ORDER BY, not the insert sequence.
    insert_event(conn, make_event(title="Slot 2", event_index_in_post=2))
    insert_event(conn, make_event(title="Slot 0", event_index_in_post=0))
    insert_event(conn, make_event(title="Slot 1", event_index_in_post=1))

    assert events_exist_for_source_url(conn, "https://meetup.example/e/1") == [0, 1, 2]


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


# ------------------------------------------------------------
# Issue #88 — Event model + repository full-domain persistence
# ------------------------------------------------------------


@pytest.mark.parametrize("category", list(get_args(EventCategory)))
def test_event_model_accepts_every_literal_category(category: str) -> None:
    """Every value in the `EventCategory` Literal must construct a valid
    `Event`. Guards against the enum quietly diverging from `Event`."""
    event = make_event(category=category)
    assert event.category == category


def test_event_model_rejects_a_category_outside_the_literal() -> None:
    """A category outside the shared Literal raises `ValidationError` at
    construction — the tool layer turns that into `invalid_event_data`."""
    with pytest.raises(ValidationError):
        make_event(category="party")


def test_event_model_defaults_the_new_domain_fields_to_nullish() -> None:
    """None of the migration-002 fields are required — the defaults reflect
    "not known": strings default to `None`, `tags` to an empty list,
    `recurring` to `False`."""
    event = make_event()
    assert event.source_account is None
    assert event.venue_name is None
    assert event.venue_address is None
    assert event.organizer is None
    assert event.tags == []
    assert event.description is None
    assert event.ticket_url is None
    assert event.image_url is None
    assert event.language is None
    assert event.recurring is False


@pytest.mark.parametrize("tags", [[], ["techno"], ["techno", "dj-set", "live-band"]])
def test_insert_event_round_trips_tags_as_a_json_array(
    conn: sqlite3.Connection, tags: list[str]
) -> None:
    """Every `Event.tags` payload survives the write→read round trip through
    the JSON-encoded `tags` TEXT column."""
    insert_event(conn, make_event(tags=tags))
    found = query_events(conn)
    assert len(found) == 1
    assert found[0].tags == tags


def test_insert_event_persists_every_new_domain_column(conn: sqlite3.Connection) -> None:
    """A fully-populated `Event` — every rich-domain field set — round-trips
    through `insert_event`/`query_events` without loss."""
    insert_event(
        conn,
        make_event(
            source_account="sala_apolo",
            venue_name="Sala Apolo",
            venue_address="Nou de la Rambla 113",
            organizer="Nitsa",
            tags=["techno", "dj-set"],
            description="Marathon night.",
            ticket_url="https://tickets.example/apolo",
            image_url="https://cdn.example/apolo.jpg",
            language="es",
            recurring=True,
        ),
    )
    found = query_events(conn)
    assert len(found) == 1
    row = found[0]
    assert row.source_account == "sala_apolo"
    assert row.venue_name == "Sala Apolo"
    assert row.venue_address == "Nou de la Rambla 113"
    assert row.organizer == "Nitsa"
    assert row.tags == ["techno", "dj-set"]
    assert row.description == "Marathon night."
    assert row.ticket_url == "https://tickets.example/apolo"
    assert row.image_url == "https://cdn.example/apolo.jpg"
    assert row.language == "es"
    assert row.recurring is True


def test_query_events_filters_by_venue_name_exact_match(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event(source_url="https://e/apolo", venue_name="Sala Apolo"))
    insert_event(conn, make_event(source_url="https://e/razz", venue_name="Razzmatazz"))
    assert {e.venue_name for e in query_events(conn, venue_name="Sala Apolo")} == {"Sala Apolo"}


def test_query_events_filters_by_tag_membership(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event(source_url="https://e/t1", tags=["techno", "dj-set"]))
    insert_event(conn, make_event(source_url="https://e/t2", tags=["jazz"]))
    insert_event(conn, make_event(source_url="https://e/t3", tags=[]))
    assert {e.source_url for e in query_events(conn, tag="techno")} == {"https://e/t1"}
    assert {e.source_url for e in query_events(conn, tag="jazz")} == {"https://e/t2"}
    assert query_events(conn, tag="ballet") == []


def test_query_events_filters_by_title_substring(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event(source_url="https://e/t1", title="AI Meetup — Barcelona"))
    insert_event(conn, make_event(source_url="https://e/t2", title="Rust Users Group"))
    assert {e.source_url for e in query_events(conn, title_contains="Meetup")} == {"https://e/t1"}


def test_query_events_filters_by_budget_cents_max_upper_bound(conn: sqlite3.Connection) -> None:
    insert_event(conn, make_event(source_url="https://e/free", price_cents=0))
    insert_event(conn, make_event(source_url="https://e/cheap", price_cents=1000))
    insert_event(conn, make_event(source_url="https://e/paid", price_cents=5000))
    assert {e.source_url for e in query_events(conn, budget_cents_max=1500)} == {
        "https://e/free",
        "https://e/cheap",
    }


# ------------------------------------------------------------
# Migration 002 — schema shape assertions
# ------------------------------------------------------------


def test_migration_002_lands_and_bumps_user_version(conn: sqlite3.Connection) -> None:
    """After `db.connect()` opens a fresh in-memory DB, `PRAGMA user_version`
    reads back as at least `2` and every migration-002 column exists on the
    `events` table."""
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 2
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    for name in (
        "source_account",
        "venue_name",
        "venue_address",
        "organizer",
        "tags",
        "description",
        "ticket_url",
        "image_url",
        "language",
        "recurring",
    ):
        assert name in columns, f"migration 002 did not add column {name!r}"


def test_migration_002_creates_the_two_composite_indexes(conn: sqlite3.Connection) -> None:
    """The two hot-path composite indexes back the Recommender's filters."""
    index_names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'events'"
        ).fetchall()
    }
    assert "idx_events_city_start" in index_names
    assert "idx_events_category_start" in index_names
