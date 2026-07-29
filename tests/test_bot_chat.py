"""Behaviour tests for the plain-text dispatch — real SQLite, recording surface.

Mirrors `tests/test_bot_registration.py`'s shape (a `RecordingSurface`, real
`data/bot.yaml`, no transport object in the path), with `conn` backed by a
`tmp_path` file rather than `":memory:"` so a connection opened on another
thread would see the same database.

The conversation turn itself is stubbed at `chat.run_conversation_turn`: what
this module locks is the *routing* into it and the rendering out of it, not the
service's own multi-turn precedence (that is `tests/test_bot_find_command.py`'s
and the `conversation/` context's own coverage).

What is locked: a mid-registration sender still advances through
`bot/registration.py` unchanged, with the provider never called (routing is
undisturbed); an unregistered sender with nothing in flight gets the
register-first reply and no turn runs (AC5); a registered sender's free text
runs one turn bound to their own internal `user_id` and the reply is rendered
through the shipped catalog (AC1); two distinct senders bind to their own ids,
never a shared or cross-wired one (AC3); a raised provider error maps to its
own reply without propagating or leaking the exception text (AC4); and a
sender mid-clarification skips the register-first gate, so an unregistered
sender can finish a `/find` conversation they already started.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from planazo.agents import loop
from planazo.bot import chat
from planazo.bot.chat import handle_plain_text
from planazo.bot.commands import format_reply
from planazo.bot.config import BotConfig, load_config, resolve_for
from planazo.bot.models import IncomingMessage
from planazo.bot.session import stored_id
from planazo.conversation.models import ConversationReply, ConversationState, PendingClarification
from planazo.conversation.repository import upsert_state
from planazo.identity import (
    UserRecord,
    get_or_create_user,
    record_registration_answer,
    set_pending_registration_field,
)
from planazo.query.models import SearchIntent
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
    text: str = "hello",
) -> IncomingMessage:
    """One valid `IncomingMessage`, with every field overridable by keyword."""
    return IncomingMessage(
        telegram_user_id=telegram_user_id,
        display_name=display_name,
        telegram_handle=telegram_handle,
        text=text,
    )


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("music",),
    )


def _register_complete(
    conn: sqlite3.Connection, telegram_user_id: str, display_name: str
) -> UserRecord:
    """A `users` row with every profile field set — one full pass through
    `bot/registration.py`'s five steps, written directly against `conn`."""
    user = get_or_create_user(conn, telegram_user_id, display_name)
    assert user.id is not None
    record_registration_answer(conn, user.id, "age", 29, None)
    record_registration_answer(conn, user.id, "location", "Barcelona", None)
    record_registration_answer(conn, user.id, "language", "en", None)
    return record_registration_answer(conn, user.id, "nationality", "Spain", None)


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A file-backed `db.DB_PATH`, never `":memory:"` — see this module's docstring."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
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
async def test_a_mid_registration_sender_still_advances_through_registration(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing is undisturbed: an in-flight registration answer reaches
    `handle_registration_answer`'s unchanged behaviour, and the provider is
    never called on this branch."""
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.id is not None
    set_pending_registration_field(conn, user.id, "age")
    monkeypatch.setattr(
        loop, "call", MagicMock(side_effect=AssertionError("the provider must not be called"))
    )

    await handle_plain_text(surface, conn, make_message(text="29"), config)

    after = get_or_create_user(conn, "tg-1", "Dani V")
    assert after.age == 29
    assert after.pending_registration_field == "location"
    (reply,) = surface.replies
    assert reply == resolve_for(config, "register_location", after)


@pytest.mark.asyncio
async def test_an_unregistered_senders_free_text_gets_the_register_first_reply(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: no pending registration and an incomplete profile is a
    register-first reply, with zero provider calls."""
    user = get_or_create_user(conn, "tg-1", "Dani V")
    monkeypatch.setattr(
        loop, "call", MagicMock(side_effect=AssertionError("the provider must not be called"))
    )

    await handle_plain_text(surface, conn, make_message(text="anything fun tonight?"), config)

    assert surface.replies == [resolve_for(config, "chat_register_first", user)]


@pytest.mark.asyncio
async def test_a_registered_senders_free_text_runs_one_turn_and_relays_the_rendered_reply(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a fully registered sender's free text reaches the conversation
    service bound to that sender's own internal id, and the reply it returns is
    rendered through the shipped catalog rather than relayed raw."""
    user = _register_complete(conn, "tg-1", "Dani V")
    seen: list[tuple[int, str]] = []

    async def fake_turn(user_id: int, text: str) -> ConversationReply:
        seen.append((user_id, text))
        return ConversationReply(kind="clarification", question="Which category?")

    monkeypatch.setattr(chat, "run_conversation_turn", fake_turn)

    await handle_plain_text(surface, conn, make_message(text="anything fun tonight?"), config)

    assert seen == [(stored_id(user), "anything fun tonight?")]
    assert surface.replies == [
        format_reply(config, ConversationReply(kind="clarification", question="Which category?"))
    ]


@pytest.mark.asyncio
async def test_two_senders_free_text_binds_to_each_ones_own_internal_user_id(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: the turn is bound to the sender's own internal id, never a
    hardcoded or cross-wired one."""
    user_a = _register_complete(conn, "tg-a", "Ada")
    user_b = _register_complete(conn, "tg-b", "Bo")
    seen: list[int] = []

    async def fake_turn(user_id: int, text: str) -> ConversationReply:
        seen.append(user_id)
        return ConversationReply(kind="no_results")

    monkeypatch.setattr(chat, "run_conversation_turn", fake_turn)

    await handle_plain_text(
        surface, conn, make_message(telegram_user_id="tg-a", text="I like techno"), config
    )
    await handle_plain_text(
        surface, conn, make_message(telegram_user_id="tg-b", text="I like jazz"), config
    )

    assert seen == [stored_id(user_a), stored_id(user_b)]
    assert stored_id(user_a) != stored_id(user_b)


@pytest.mark.asyncio
async def test_a_raised_provider_error_maps_to_its_own_reply_without_propagating(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: an exception on the way to the provider is caught and mapped to the
    configured reply — it never escapes `handle_plain_text`, and the exception's
    own text never reaches the user."""
    user = _register_complete(conn, "tg-1", "Dani V")

    async def failing_turn(user_id: int, text: str) -> ConversationReply:
        raise RuntimeError("the provider is unreachable")

    monkeypatch.setattr(chat, "run_conversation_turn", failing_turn)

    await handle_plain_text(surface, conn, make_message(text="anything fun tonight?"), config)

    assert surface.replies == [resolve_for(config, "chat_provider_error", user)]
    assert "unreachable" not in surface.replies[0]


@pytest.mark.asyncio
async def test_a_mid_clarification_sender_skips_the_register_first_gate(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered sender who already ran `/find` and got a clarification
    can still answer it: gating that answer would wedge the conversation with
    no way to finish it. The register-first reply is what this must NOT be."""
    user = get_or_create_user(conn, "tg-1", "Dani V")
    assert user.id is not None
    assert not user.profile_complete
    upsert_state(
        conn,
        ConversationState(
            user_id=user.id,
            pending_clarification=PendingClarification(
                question="Which category?", intent_snapshot=_intent()
            ),
            updated_at=datetime.now(UTC),
        ),
    )
    dispatched: list[int] = []

    async def fake_turn(user_id: int, text: str) -> ConversationReply:
        dispatched.append(user_id)
        return ConversationReply(kind="no_results")

    monkeypatch.setattr(chat, "run_conversation_turn", fake_turn)

    await handle_plain_text(surface, conn, make_message(text="music"), config)

    assert dispatched == [user.id]
    assert surface.replies != [resolve_for(config, "chat_register_first", user)]
