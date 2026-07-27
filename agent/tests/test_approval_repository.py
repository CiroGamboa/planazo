import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from planazo.approval import ApprovalDecision, list_approvals, record_approval
from planazo.catalog import Event, insert_event
from planazo.identity import get_or_create_user
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
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_record_approval_round_trips_through_list_approvals(conn: sqlite3.Connection) -> None:
    user = get_or_create_user(conn, "tg-1", "Dani")
    assert user.id is not None
    event_db_id = insert_event(conn, make_event())

    approval_id = record_approval(
        conn,
        ApprovalDecision(
            user_id=user.id, artifact_kind="event", artifact_id=event_db_id, decision="approve"
        ),
    )

    stored = list_approvals(conn, user.id)
    assert len(stored) == 1
    assert stored[0].id == approval_id
    assert stored[0].decision == "approve"
    assert stored[0].artifact_id == event_db_id
    assert stored[0].decided_at is not None
