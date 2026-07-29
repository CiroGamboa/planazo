"""Curator LLM tools — six tools, typed error branches, dry-run semantics.

Every tool takes flat scalar arguments, opens its own connection, and
returns a `dict[str, object]` with either the happy-path shape or a
`{"error_type": ..., "message": ...}` branch. No exception escapes.

Tests exercise: happy path for each tool, every declared error branch,
and the dry-run flip via `build_curator_tools(dry_run=True)`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from planazo.catalog import Event, get_event_by_id, insert_event
from planazo.catalog.repository import soft_delete_event
from planazo.curator.tools import (
    REASON_CAP,
    build_curator_tools,
    list_duplicate_candidates,
    list_low_confidence_events,
    list_stale_events,
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


class _NoCloseConn:
    """Proxy that forwards every attribute except `close()`.

    The tool tier calls `db.connect()` and then `conn.close()` in a
    `finally` block. Tests want one shared in-memory DB across many tool
    invocations, so a close on the shared connection would drop the whole
    schema mid-test. This proxy makes `close()` a no-op while forwarding
    every other attribute to the wrapped connection.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def close(self) -> None:  # noqa: D401 — no-op proxy
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.fixture
def tmp_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """Point every `db.connect()` call at a shared in-memory DB.

    Each `db.connect()` in the tool tier opens its own connection and
    closes it in a `finally` block; tests want one shared connection so
    seeded rows survive across tool invocations. The fixture wraps the
    real connection in `_NoCloseConn` so tool-tier `close()` calls are
    no-ops.
    """
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    proxy = _NoCloseConn(connection)
    monkeypatch.setattr(db, "connect", lambda: proxy)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# list_stale_events
# ---------------------------------------------------------------------------


def test_list_stale_events_returns_past_events_only(tmp_db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    stale_id = insert_event(
        tmp_db,
        make_event(
            source_url="https://seed/past",
            start_utc=now - timedelta(days=10),
            end_utc=now - timedelta(days=9),
        ),
    )
    insert_event(
        tmp_db,
        make_event(
            source_url="https://seed/future",
            start_utc=now + timedelta(days=1),
            end_utc=now + timedelta(days=2),
        ),
    )

    result = list_stale_events()

    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["event_id"] == stale_id
    assert events[0]["days_past"] >= 9


def test_list_stale_events_ignores_archived_rows(tmp_db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    archived_stale = insert_event(
        tmp_db,
        make_event(
            source_url="https://seed/archived-past",
            start_utc=now - timedelta(days=10),
            end_utc=now - timedelta(days=9),
        ),
    )
    soft_delete_event(tmp_db, archived_stale)

    result = list_stale_events()

    assert result["total"] == 0


def test_list_stale_events_rejects_bad_limit() -> None:
    outcome = list_stale_events(limit=0)

    assert outcome["error_type"] == "invalid_search_filter"


# ---------------------------------------------------------------------------
# list_duplicate_candidates
# ---------------------------------------------------------------------------


def test_list_duplicate_candidates_groups_by_title_date_venue(
    tmp_db: sqlite3.Connection,
) -> None:
    when = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    first = insert_event(
        tmp_db,
        make_event(
            source_url="https://a/1",
            title="  Techno Night  ",
            start_utc=when,
            end_utc=when + timedelta(hours=2),
            venue_name="Sala Apolo",
            category="music",
        ),
    )
    second = insert_event(
        tmp_db,
        make_event(
            source_url="https://b/1",
            title="techno night",
            start_utc=when + timedelta(minutes=30),
            end_utc=when + timedelta(hours=3),
            venue_name="Sala Apolo",
            category="music",
        ),
    )
    # A third event — different day, should NOT group.
    insert_event(
        tmp_db,
        make_event(
            source_url="https://c/1",
            title="Techno Night",
            start_utc=when + timedelta(days=1),
            end_utc=when + timedelta(days=1, hours=2),
            venue_name="Sala Apolo",
            category="music",
        ),
    )

    result = list_duplicate_candidates()

    assert result["total"] == 1
    groups = result["groups"]
    assert isinstance(groups, list)
    ids = {event["event_id"] for event in groups[0]["events"]}
    assert ids == {first, second}


def test_list_duplicate_candidates_ignores_archived(tmp_db: sqlite3.Connection) -> None:
    when = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    live_id = insert_event(
        tmp_db,
        make_event(
            source_url="https://a/1",
            title="Duplicate",
            start_utc=when,
            end_utc=when + timedelta(hours=2),
            venue_name="V1",
        ),
    )
    archived_id = insert_event(
        tmp_db,
        make_event(
            source_url="https://b/1",
            title="Duplicate",
            start_utc=when + timedelta(minutes=15),
            end_utc=when + timedelta(hours=3),
            venue_name="V1",
        ),
    )
    soft_delete_event(tmp_db, archived_id)

    result = list_duplicate_candidates()

    assert result["total"] == 0
    # Sanity — the live row is still there.
    assert get_event_by_id(tmp_db, live_id) is not None


def test_list_duplicate_candidates_rejects_bad_limit() -> None:
    outcome = list_duplicate_candidates(limit=0)

    assert outcome["error_type"] == "invalid_search_filter"


# ---------------------------------------------------------------------------
# list_low_confidence_events
# ---------------------------------------------------------------------------


def test_list_low_confidence_events_returns_below_threshold(
    tmp_db: sqlite3.Connection,
) -> None:
    high = insert_event(tmp_db, make_event(source_url="https://a/1", confidence=0.9))
    low = insert_event(tmp_db, make_event(source_url="https://a/2", confidence=0.3))
    borderline = insert_event(tmp_db, make_event(source_url="https://a/3", confidence=0.4))

    result = list_low_confidence_events(threshold=0.4)

    assert result["total"] == 1
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["event_id"] == low
    ids = {event["event_id"] for event in events}
    assert high not in ids
    assert borderline not in ids


def test_list_low_confidence_events_rejects_threshold_out_of_range() -> None:
    assert list_low_confidence_events(threshold=-0.1)["error_type"] == "invalid_search_filter"
    assert list_low_confidence_events(threshold=1.5)["error_type"] == "invalid_search_filter"


# ---------------------------------------------------------------------------
# archive_event
# ---------------------------------------------------------------------------


def test_archive_event_soft_deletes_and_returns_ok(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=event_id, reason="past event")

    assert outcome == {"status": "ok", "archived_event_id": event_id}
    assert get_event_by_id(tmp_db, event_id) is None
    assert get_event_by_id(tmp_db, event_id, include_archived=True) is not None


def test_archive_event_dry_run_returns_dry_run_and_does_not_mutate(
    tmp_db: sqlite3.Connection,
) -> None:
    event_id = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=True)

    outcome = tools["archive_event"](event_id=event_id, reason="past event")

    assert outcome == {"status": "dry_run", "archived_event_id": event_id}
    row = get_event_by_id(tmp_db, event_id)
    assert row is not None
    assert row.archived_at is None


def test_archive_event_refuses_missing_id(tmp_db: sqlite3.Connection) -> None:
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=999_999, reason="does not exist")

    assert outcome["error_type"] == "not_found"


def test_archive_event_refuses_already_archived(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event())
    soft_delete_event(tmp_db, event_id)
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=event_id, reason="second attempt")

    assert outcome["error_type"] == "already_archived"


def test_archive_event_refuses_empty_reason(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=event_id, reason="   ")

    assert outcome["error_type"] == "invalid_reason"


def test_archive_event_refuses_reason_over_cap(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=event_id, reason="x" * (REASON_CAP + 1))

    assert outcome["error_type"] == "invalid_reason"


def test_archive_event_refuses_invalid_id(tmp_db: sqlite3.Connection) -> None:
    tools = build_curator_tools(dry_run=False)

    outcome = tools["archive_event"](event_id=0, reason="bad id")

    assert outcome["error_type"] == "invalid_event_id"


# ---------------------------------------------------------------------------
# merge_events
# ---------------------------------------------------------------------------


def test_merge_events_archives_all_ids_and_keeps_the_canonical_one(
    tmp_db: sqlite3.Connection,
) -> None:
    when = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    keeper = insert_event(
        tmp_db,
        make_event(
            source_url="https://keep/1",
            start_utc=when,
            end_utc=when + timedelta(hours=2),
        ),
    )
    dup_a = insert_event(
        tmp_db,
        make_event(
            source_url="https://dup/a",
            start_utc=when,
            end_utc=when + timedelta(hours=2),
        ),
    )
    dup_b = insert_event(
        tmp_db,
        make_event(
            source_url="https://dup/b",
            start_utc=when,
            end_utc=when + timedelta(hours=2),
        ),
    )
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[dup_a, dup_b], reason="same event"
    )

    assert outcome["status"] == "ok"
    assert outcome["kept_event_id"] == keeper
    assert set(outcome["archived_event_ids"]) == {dup_a, dup_b}
    assert get_event_by_id(tmp_db, keeper) is not None
    assert get_event_by_id(tmp_db, dup_a) is None
    assert get_event_by_id(tmp_db, dup_b) is None


def test_merge_events_dry_run_does_not_mutate(tmp_db: sqlite3.Connection) -> None:
    keeper = insert_event(tmp_db, make_event(source_url="https://keep/1"))
    dup = insert_event(tmp_db, make_event(source_url="https://dup/1"))
    tools = build_curator_tools(dry_run=True)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[dup], reason="same event"
    )

    assert outcome["status"] == "dry_run"
    assert get_event_by_id(tmp_db, dup) is not None


def test_merge_events_refuses_keep_id_in_archive_list(tmp_db: sqlite3.Connection) -> None:
    keeper = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[keeper], reason="self-loop"
    )

    assert outcome["error_type"] == "invalid_merge_group"


def test_merge_events_refuses_empty_archive_list(tmp_db: sqlite3.Connection) -> None:
    keeper = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[], reason="nothing to archive"
    )

    assert outcome["error_type"] == "invalid_merge_group"


def test_merge_events_refuses_missing_archive_id(tmp_db: sqlite3.Connection) -> None:
    keeper = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[999_999], reason="missing id"
    )

    assert outcome["error_type"] == "not_found"


def test_merge_events_refuses_missing_keeper(tmp_db: sqlite3.Connection) -> None:
    dup = insert_event(tmp_db, make_event())
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=999_999, archive_event_ids=[dup], reason="missing keeper"
    )

    assert outcome["error_type"] == "invalid_merge_group"
    # The dup was NOT archived because the whole call was refused.
    assert get_event_by_id(tmp_db, dup) is not None


def test_merge_events_refuses_already_archived_target(tmp_db: sqlite3.Connection) -> None:
    keeper = insert_event(tmp_db, make_event(source_url="https://keep/1"))
    dup = insert_event(tmp_db, make_event(source_url="https://dup/1"))
    soft_delete_event(tmp_db, dup)
    tools = build_curator_tools(dry_run=False)

    outcome = tools["merge_events"](
        keep_event_id=keeper, archive_event_ids=[dup], reason="already gone"
    )

    assert outcome["error_type"] == "already_archived"


# ---------------------------------------------------------------------------
# update_event_category
# ---------------------------------------------------------------------------


def test_update_event_category_changes_and_verifies_read_back(
    tmp_db: sqlite3.Connection,
) -> None:
    event_id = insert_event(tmp_db, make_event(category="tech"))
    tools = build_curator_tools(dry_run=False)

    outcome = tools["update_event_category"](
        event_id=event_id, new_category="cultural", reason="mis-classified"
    )

    assert outcome["status"] == "ok"
    assert outcome["old_category"] == "tech"
    assert outcome["new_category"] == "cultural"
    row = get_event_by_id(tmp_db, event_id)
    assert row is not None
    assert row.category == "cultural"


def test_update_event_category_dry_run_returns_shape_without_mutation(
    tmp_db: sqlite3.Connection,
) -> None:
    event_id = insert_event(tmp_db, make_event(category="tech"))
    tools = build_curator_tools(dry_run=True)

    outcome = tools["update_event_category"](
        event_id=event_id, new_category="cultural", reason="mis-classified"
    )

    assert outcome["status"] == "dry_run"
    assert outcome["old_category"] == "tech"
    assert outcome["new_category"] == "cultural"
    row = get_event_by_id(tmp_db, event_id)
    assert row is not None
    assert row.category == "tech"


def test_update_event_category_refuses_invalid_literal(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event(category="tech"))
    tools = build_curator_tools(dry_run=False)

    outcome = tools["update_event_category"](
        event_id=event_id, new_category="party", reason="not a Literal"
    )

    assert outcome["error_type"] == "invalid_category"
    row = get_event_by_id(tmp_db, event_id)
    assert row is not None
    assert row.category == "tech"


def test_update_event_category_returns_no_change_needed_when_already_matches(
    tmp_db: sqlite3.Connection,
) -> None:
    event_id = insert_event(tmp_db, make_event(category="tech"))
    tools = build_curator_tools(dry_run=False)

    outcome = tools["update_event_category"](
        event_id=event_id, new_category="tech", reason="already right"
    )

    assert outcome["error_type"] == "no_change_needed"


def test_update_event_category_refuses_archived(tmp_db: sqlite3.Connection) -> None:
    event_id = insert_event(tmp_db, make_event(category="tech"))
    soft_delete_event(tmp_db, event_id)
    tools = build_curator_tools(dry_run=False)

    outcome = tools["update_event_category"](
        event_id=event_id, new_category="cultural", reason="edit archived"
    )

    assert outcome["error_type"] == "already_archived"


def test_update_event_category_refuses_missing_id(tmp_db: sqlite3.Connection) -> None:
    tools = build_curator_tools(dry_run=False)

    outcome = tools["update_event_category"](
        event_id=999_999, new_category="cultural", reason="ghost id"
    )

    assert outcome["error_type"] == "not_found"


# ---------------------------------------------------------------------------
# build_curator_tools registry
# ---------------------------------------------------------------------------


def test_build_curator_tools_returns_expected_registry() -> None:
    tools = build_curator_tools(dry_run=False)

    assert set(tools.keys()) == {
        "list_stale_events",
        "list_duplicate_candidates",
        "list_low_confidence_events",
        "archive_event",
        "merge_events",
        "update_event_category",
    }
