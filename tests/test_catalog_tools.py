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
