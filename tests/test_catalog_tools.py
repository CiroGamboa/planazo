from pathlib import Path

import pytest

from planazo.catalog import save_event, search_events
from planazo.storage import db
from tools.schema import schema_for


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


def test_save_event_persists_and_returns_the_row_id(db_file: Path) -> None:
    result = save_event(
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
    result = save_event(
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
    first = save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    second = save_event(
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
    found = search_events()
    assert found["total"] == 1
    events = found["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "AI Meetup"


def test_save_event_persists_two_slots_for_the_same_source_url(db_file: Path) -> None:
    first = save_event(
        title="Carousel — night 1",
        category="music",
        source="instagram",
        source_url="https://www.instagram.com/p/carousel/",
        start_utc="2026-08-01T21:00:00+00:00",
        end_utc="2026-08-01T23:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        event_index_in_post=0,
    )
    second = save_event(
        title="Carousel — night 2",
        category="music",
        source="instagram",
        source_url="https://www.instagram.com/p/carousel/",
        start_utc="2026-08-02T21:00:00+00:00",
        end_utc="2026-08-02T23:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        event_index_in_post=1,
    )

    assert isinstance(first["event_db_id"], int)
    assert isinstance(second["event_db_id"], int)
    assert first["event_db_id"] != second["event_db_id"]

    found = search_events(category="music")
    assert found["total"] == 2


def test_save_event_reports_duplicate_when_source_url_and_slot_both_repeat(
    db_file: Path,
) -> None:
    first = save_event(
        title="Carousel — night 2",
        category="music",
        source="instagram",
        source_url="https://www.instagram.com/p/carousel/",
        start_utc="2026-08-02T21:00:00+00:00",
        end_utc="2026-08-02T23:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        event_index_in_post=1,
    )
    duplicate = save_event(
        title="Carousel — night 2 (retry)",
        category="music",
        source="instagram",
        source_url="https://www.instagram.com/p/carousel/",
        start_utc="2026-08-02T21:00:00+00:00",
        end_utc="2026-08-02T23:00:00+00:00",
        city="Barcelona",
        confidence=0.6,
        event_index_in_post=1,
    )

    assert duplicate["error_type"] == "duplicate_event"
    assert duplicate["event_db_id"] == first["event_db_id"]


def test_save_event_rejects_negative_event_index_in_post(db_file: Path) -> None:
    result = save_event(
        title="Bad slot",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        event_index_in_post=-1,
    )

    assert result["error_type"] == "invalid_event_data"
    message = result["message"]
    assert isinstance(message, str)
    assert "event_index_in_post" in message
    assert not db_file.exists()


def test_search_events_finds_a_row_saved_by_a_separate_call(db_file: Path) -> None:
    save_event(
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
    result = search_events(category="tech", city="Barcelona")

    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["source_url"] == "https://meetup.example/e/1"


def test_search_events_returns_empty_for_no_matches(db_file: Path) -> None:
    save_event(
        title="AI Meetup",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/1",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    assert search_events(category="music")["total"] == 0


def test_search_events_reports_an_unparseable_start_after(db_file: Path) -> None:
    result = search_events(start_after="tonight")

    assert result["error_type"] == "invalid_search_filter"


def test_search_events_respects_max_results(db_file: Path) -> None:
    for source_url, start in (
        ("https://e/1", "2026-08-01T19:00:00+00:00"),
        ("https://e/2", "2026-08-02T19:00:00+00:00"),
        ("https://e/3", "2026-08-03T19:00:00+00:00"),
    ):
        save_event(
            title="AI Meetup",
            category="tech",
            source="meetup",
            source_url=source_url,
            start_utc=start,
            end_utc="2026-08-05T21:00:00+00:00",
            city="Barcelona",
            confidence=0.9,
        )

    assert search_events(max_results=2)["total"] == 2


@pytest.mark.parametrize("max_results", [0, -1])
def test_search_events_rejects_a_non_positive_max_results(db_file: Path, max_results: int) -> None:
    result = search_events(max_results=max_results)

    assert result["error_type"] == "invalid_search_filter"


def test_schema_for_covers_both_catalog_tools() -> None:
    for tool in (save_event, search_events):
        schema = schema_for(tool)
        assert schema["name"] == tool.__name__
        assert "description" in schema
        assert "parameters" in schema


def test_save_event_docstring_does_not_point_at_search_events() -> None:
    """Regression guard for issue #9: `save_event`'s docstring must not
    dangle a `search_events` pointer — the Extractor's registry does not
    include `search_events`, and the pointer would trigger
    `tool_failed: unknown tool: search_events`."""
    assert save_event.__doc__ is not None
    assert "use search_events" not in save_event.__doc__


# ------------------------------------------------------------
# Issue #88 — full-domain columns round-trip through the tools
# ------------------------------------------------------------


def test_save_event_persists_every_new_domain_field(db_file: Path) -> None:
    """A `save_event` call with every rich-domain field set — every one
    survives the round trip through `search_events`."""
    result = save_event(
        title="Techno all-nighter",
        category="music",
        source="instagram",
        source_url="https://www.instagram.com/p/apolo/",
        start_utc="2026-09-05T23:00:00+00:00",
        end_utc="2026-09-06T06:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        price_cents=1500,
        source_account="sala_apolo",
        venue_name="Sala Apolo",
        venue_address="Nou de la Rambla 113",
        organizer="Nitsa",
        tags=["techno", "dj-set"],
        description="Marathon techno night with three headliners.",
        ticket_url="https://tickets.example/apolo",
        image_url="https://cdn.example/apolo.jpg",
        language="es",
        recurring=True,
    )

    assert "error_type" not in result
    saved = result["saved"]
    assert isinstance(saved, dict)

    found = search_events(category="music")
    assert found["total"] == 1
    events = found["events"]
    assert isinstance(events, list)
    row = events[0]
    assert row["source_account"] == "sala_apolo"
    assert row["venue_name"] == "Sala Apolo"
    assert row["venue_address"] == "Nou de la Rambla 113"
    assert row["organizer"] == "Nitsa"
    assert row["tags"] == ["techno", "dj-set"]
    assert row["description"] == "Marathon techno night with three headliners."
    assert row["ticket_url"] == "https://tickets.example/apolo"
    assert row["image_url"] == "https://cdn.example/apolo.jpg"
    assert row["language"] == "es"
    assert row["recurring"] is True


def test_save_event_rejects_a_category_outside_the_literal(db_file: Path) -> None:
    """`Event.category` is the `EventCategory` Literal — an off-set value
    (`"party"`) round-trips as `invalid_event_data`, never as a written row."""
    result = save_event(
        title="Bad category",
        category="party",  # type: ignore[arg-type]
        source="meetup",
        source_url="https://meetup.example/e/x",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    assert result["error_type"] == "invalid_event_data"
    assert search_events()["total"] == 0


def test_search_events_filters_by_venue_name_exact_match(db_file: Path) -> None:
    save_event(
        title="Apolo night",
        category="music",
        source="instagram",
        source_url="https://ig.example/1",
        start_utc="2026-09-01T22:00:00+00:00",
        end_utc="2026-09-02T04:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        venue_name="Sala Apolo",
    )
    save_event(
        title="Razz night",
        category="music",
        source="instagram",
        source_url="https://ig.example/2",
        start_utc="2026-09-02T22:00:00+00:00",
        end_utc="2026-09-03T04:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        venue_name="Razzmatazz",
    )

    result = search_events(venue_name="Sala Apolo")
    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "Apolo night"


def test_search_events_filters_by_tag_json_membership(db_file: Path) -> None:
    save_event(
        title="Techno night",
        category="music",
        source="instagram",
        source_url="https://ig.example/tag/1",
        start_utc="2026-09-01T22:00:00+00:00",
        end_utc="2026-09-02T04:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        tags=["techno", "dj-set"],
    )
    save_event(
        title="Jazz evening",
        category="music",
        source="instagram",
        source_url="https://ig.example/tag/2",
        start_utc="2026-09-02T22:00:00+00:00",
        end_utc="2026-09-03T04:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        tags=["jazz", "live-band"],
    )

    result = search_events(tag="techno")
    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "Techno night"


def test_search_events_filters_by_title_substring(db_file: Path) -> None:
    save_event(
        title="AI Meetup — Barcelona",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/ai",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )
    save_event(
        title="Rust Users Group",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/rust",
        start_utc="2026-08-02T19:00:00+00:00",
        end_utc="2026-08-02T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    result = search_events(title_contains="Meetup")
    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "AI Meetup — Barcelona"


def test_search_events_filters_by_budget_cents_max(db_file: Path) -> None:
    save_event(
        title="Free talk",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/free",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        price_cents=0,
    )
    save_event(
        title="Paid conference",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/paid",
        start_utc="2026-08-02T19:00:00+00:00",
        end_utc="2026-08-02T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        price_cents=5000,
    )

    result = search_events(budget_cents_max=1000)
    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["title"] == "Free talk"


def test_search_events_returns_all_rows_when_no_filters_supplied(db_file: Path) -> None:
    """The sentinel defaults — `""` for str, `-1` for `budget_cents_max` —
    must all read as "no filter" and hand back every row."""
    save_event(
        title="Free talk",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/free",
        start_utc="2026-08-01T19:00:00+00:00",
        end_utc="2026-08-01T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        price_cents=0,
    )
    save_event(
        title="Paid conference",
        category="tech",
        source="meetup",
        source_url="https://meetup.example/e/paid",
        start_utc="2026-08-02T19:00:00+00:00",
        end_utc="2026-08-02T21:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
        price_cents=5000,
    )

    result = search_events()
    assert result["total"] == 2


def test_search_events_rejects_a_budget_cents_max_below_the_sentinel(db_file: Path) -> None:
    result = search_events(budget_cents_max=-5)

    assert result["error_type"] == "invalid_search_filter"
