"""Behaviour tests for the registration flow — real SQLite, recording surface.

No transport object is in the path, matching `tests/test_bot_commands.py`'s
shape: `handle_register` and `handle_registration_answer` take a
`UserSurface`, a connection, an `IncomingMessage`, and a `BotConfig`. `config`
loads the real shipped catalog, so every assertion below is proof against the
data the running bot would actually load.

What is locked: a full run through all five steps persists every field
(AC1); a bad answer per constraint kind re-prompts and writes nothing (AC2);
re-registering an already-complete profile updates the same row (AC3); a
fresh plain message after an abandoned flow lands on the right step with no
reset call in between (AC6); a plain message with nothing pending is inert;
and `/register` re-sends the pending step's own prompt rather than
restarting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from planazo.bot.config import BotConfig, load_config, resolve_for
from planazo.bot.models import IncomingMessage
from planazo.bot.registration import handle_register, handle_registration_answer
from planazo.identity import get_or_create_user, set_pending_registration_field
from planazo.storage import db


class RecordingSurface:
    """A `UserSurface` that keeps each reply instead of sending it."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


def make_message(
    *,
    telegram_user_id: str = "tg-1",
    display_name: str = "Dani V",
    telegram_handle: str | None = "daniv",
    text: str = "/register",
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


@pytest.fixture
def surface() -> RecordingSurface:
    return RecordingSurface()


@pytest.fixture
def config() -> BotConfig:
    return load_config(Path("data/bot.yaml"))


@pytest.mark.asyncio
async def test_register_walks_all_five_steps_and_persists_every_answer(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    await handle_register(surface, conn, make_message(), config)

    await handle_registration_answer(surface, conn, make_message(text="Dani"), config)
    await handle_registration_answer(surface, conn, make_message(text="29"), config)
    await handle_registration_answer(surface, conn, make_message(text="Barcelona"), config)
    # A language answer outside the catalog re-prompts before a valid one lands.
    await handle_registration_answer(surface, conn, make_message(text="fr"), config)
    await handle_registration_answer(surface, conn, make_message(text="en"), config)
    await handle_registration_answer(surface, conn, make_message(text="Spain"), config)

    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert (user.display_name, user.age, user.location, user.language, user.nationality) == (
        "Dani",
        29,
        "Barcelona",
        "en",
        "Spain",
    )
    assert user.pending_registration_field is None
    assert user.profile_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending_field", "invalid_text", "message_id", "message_kwargs"),
    [
        ("age", "not-a-number", "register_invalid_int_range", {"minimum": 13, "maximum": 120}),
        ("age", "999", "register_invalid_int_range", {"minimum": 13, "maximum": 120}),
        ("language", "fr", "register_invalid_locale", {"locales": "en, es"}),
    ],
)
async def test_an_invalid_answer_reprompts_and_writes_nothing(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    pending_field: str,
    invalid_text: str,
    message_id: str,
    message_kwargs: dict[str, object],
) -> None:
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.id is not None
    set_pending_registration_field(conn, user.id, pending_field)

    await handle_registration_answer(surface, conn, make_message(text=invalid_text), config)

    (reply,) = surface.replies
    assert reply == resolve_for(config, message_id, user, **message_kwargs)
    after = get_or_create_user(conn, "tg-1", "Dani V")
    assert getattr(after, pending_field) is None
    assert after.pending_registration_field == pending_field


@pytest.mark.asyncio
async def test_a_whitespace_only_answer_reprompts_and_leaves_the_field_unchanged(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.id is not None
    set_pending_registration_field(conn, user.id, "display_name")

    await handle_registration_answer(surface, conn, make_message(text="   "), config)

    (reply,) = surface.replies
    assert reply == resolve_for(config, "register_invalid_text", user, min_length=1, max_length=80)
    after = get_or_create_user(conn, "tg-1", "Dani V")
    assert after.display_name == "Dani V"
    assert after.pending_registration_field == "display_name"


@pytest.mark.asyncio
async def test_registering_again_after_completion_updates_the_same_row(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    await handle_register(surface, conn, make_message(), config)
    for text in ("Dani", "29", "Barcelona", "en", "Spain"):
        await handle_registration_answer(surface, conn, make_message(text=text), config)
    before_ids = [row["id"] for row in conn.execute("SELECT id FROM users")]

    await handle_register(surface, conn, make_message(), config)
    for text in ("Dani V", "31", "Madrid", "es", "France"):
        await handle_registration_answer(surface, conn, make_message(text=text), config)

    assert [row["id"] for row in conn.execute("SELECT id FROM users")] == before_ids
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert (user.display_name, user.age, user.location, user.language, user.nationality) == (
        "Dani V",
        31,
        "Madrid",
        "es",
        "France",
    )


@pytest.mark.asyncio
async def test_a_fresh_plain_message_after_two_answers_targets_the_third_step(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    await handle_register(surface, conn, make_message(), config)
    await handle_registration_answer(surface, conn, make_message(text="Dani"), config)
    await handle_registration_answer(surface, conn, make_message(text="29"), config)

    # No `/register` call in between — the next plain message alone must
    # resume at the third step, simulating a dropped-and-reconnected client.
    await handle_registration_answer(surface, conn, make_message(text="Barcelona"), config)

    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.location == "Barcelona"
    assert user.pending_registration_field == "language"


@pytest.mark.asyncio
async def test_a_plain_message_with_no_registration_pending_is_inert(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    get_or_create_user(conn, "tg-1", "Dani V")

    await handle_registration_answer(surface, conn, make_message(text="hello there"), config)

    assert surface.replies == []
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.age is None
    assert user.pending_registration_field is None


@pytest.mark.asyncio
async def test_register_called_again_while_pending_resends_the_same_prompt(
    conn: sqlite3.Connection, surface: RecordingSurface, config: BotConfig
) -> None:
    await handle_register(surface, conn, make_message(), config)
    await handle_registration_answer(surface, conn, make_message(text="Dani"), config)
    surface.replies.clear()

    await handle_register(surface, conn, make_message(), config)

    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.pending_registration_field == "age"
    (reply,) = surface.replies
    assert reply == resolve_for(config, "register_age", user)
