"""Unit tests for `scheduler.repository.bootstrap_system_user`.

The scheduler attributes every `extract_once` call to a synthetic `users`
row keyed on `telegram_user_id="system"`. `bootstrap_system_user` seeds
that row at the start of every tick; the primitive underneath is
`identity.repository.get_or_create_user`, which is already idempotent by
`telegram_user_id`. These tests lock the two behaviours the scheduler
depends on: (1) the row lands with the exact identifier + display name,
(2) a second call returns the same `id` without duplicating.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from planazo.scheduler.repository import (
    SYSTEM_USER_DISPLAY_NAME,
    SYSTEM_USER_TELEGRAM_ID,
    bootstrap_system_user,
)
from planazo.storage import db


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_bootstrap_system_user_creates_row_first_time(conn: sqlite3.Connection) -> None:
    record = bootstrap_system_user(conn)
    assert record.id is not None
    assert record.telegram_user_id == SYSTEM_USER_TELEGRAM_ID
    assert record.display_name == SYSTEM_USER_DISPLAY_NAME


def test_bootstrap_system_user_idempotent(conn: sqlite3.Connection) -> None:
    first = bootstrap_system_user(conn)
    second = bootstrap_system_user(conn)
    assert first.id == second.id

    count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE telegram_user_id = ?",
        (SYSTEM_USER_TELEGRAM_ID,),
    ).fetchone()[0]
    assert count == 1


def test_bootstrap_system_user_does_not_rename_existing_row(conn: sqlite3.Connection) -> None:
    # `get_or_create_user` under the hood is get-or-create, not upsert. A
    # display-name drift in the seed constant should not silently overwrite
    # an existing row (that would let an accidental rename land through the
    # scheduler as a side effect).
    first = bootstrap_system_user(conn)
    conn.execute(
        "UPDATE users SET display_name = ? WHERE telegram_user_id = ?",
        ("Renamed By Test", SYSTEM_USER_TELEGRAM_ID),
    )
    conn.commit()

    second = bootstrap_system_user(conn)
    assert second.id == first.id
    row_name = conn.execute(
        "SELECT display_name FROM users WHERE telegram_user_id = ?",
        (SYSTEM_USER_TELEGRAM_ID,),
    ).fetchone()[0]
    assert row_name == "Renamed By Test"
