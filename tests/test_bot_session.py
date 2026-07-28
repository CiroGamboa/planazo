"""Session mapping: a Telegram sender resolved to one `users` row.

Real SQLite through the same `:memory:` connection fixture the identity
repository tests use — `resolve_user` takes an explicit connection and never
opens one, so a private in-memory database is the whole story here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from planazo.bot.models import IncomingMessage
from planazo.bot.session import resolve_user, stored_id
from planazo.identity import UserRecord
from planazo.storage import db


def make_message(
    *,
    telegram_user_id: str = "tg-1",
    display_name: str = "Dani V",
    telegram_handle: str | None = "daniv",
    text: str = "/start",
) -> IncomingMessage:
    """One valid `IncomingMessage`, with every field overridable by keyword."""
    return IncomingMessage(
        telegram_user_id=telegram_user_id,
        display_name=display_name,
        telegram_handle=telegram_handle,
        text=text,
    )


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    connection = db.connect()
    yield connection
    connection.close()


def test_first_contact_creates_the_row(conn: sqlite3.Connection) -> None:
    user = resolve_user(conn, make_message())

    assert user.id is not None
    assert user.telegram_user_id == "tg-1"
    assert user.display_name == "Dani V"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_a_renamed_sender_keeps_their_row_and_stored_name(conn: sqlite3.Connection) -> None:
    # Get-or-create, not upsert: the id is the point of the mapping, and a
    # display name that changed on Telegram must not fork a second identity.
    first = resolve_user(conn, make_message())
    second = resolve_user(conn, make_message(display_name="Dani Renamed"))

    assert second.id == first.id
    assert second.display_name == "Dani V"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_two_senders_get_two_rows(conn: sqlite3.Connection) -> None:
    first = resolve_user(conn, make_message(telegram_user_id="tg-1"))
    second = resolve_user(conn, make_message(telegram_user_id="tg-2"))

    assert first.id != second.id
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_stored_id_raises_for_an_unsaved_record() -> None:
    unsaved = UserRecord(id=None, telegram_user_id="tg-1", display_name="Dani V")

    with pytest.raises(RuntimeError):
        stored_id(unsaved)


def test_stored_id_returns_the_id_of_a_persisted_record(conn: sqlite3.Connection) -> None:
    user = resolve_user(conn, make_message())

    assert stored_id(user) == user.id
