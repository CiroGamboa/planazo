"""Behavior tests for `handle_find`.

The bot layer is transport-neutral (ADR 0011) — every command is a
coroutine taking `(UserSurface, sqlite3.Connection, IncomingMessage,
BotConfig)`, which is exactly what makes these tiers testable offline
with a recording surface. `handle_user_message` in the conversation
service is mocked here so the tests exercise only the routing +
formatting seam; the multi-turn logic itself is covered by
`test_conversation_service.py`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from planazo.bot import commands
from planazo.bot.commands import handle_find
from planazo.bot.config import BotConfig, load_config
from planazo.bot.models import IncomingMessage
from planazo.catalog.models import Event
from planazo.conversation.models import (
    ConversationReply,
)
from planazo.query.models import SearchIntent
from planazo.storage import db


class RecordingSurface:
    """Records replies rather than sending them — same shape as `test_bot_commands`."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


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


def _msg(text: str = "/find music tonight") -> IncomingMessage:
    return IncomingMessage(
        telegram_user_id="tg-1",
        display_name="Dani",
        telegram_handle="daniv",
        text=text,
    )


def _event() -> Event:
    return Event(
        id=1,
        source="seed",
        source_url="seed://event/1",
        title="Live jazz",
        start_utc=datetime(2026, 8, 1, 20, tzinfo=UTC),
        end_utc=datetime(2026, 8, 1, 22, tzinfo=UTC),
        category="music",
        city="Barcelona",
        price_cents=1500,
        confidence=0.9,
        venue_name="Sala Apolo",
    )


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("music",),
    )


@pytest.mark.asyncio
async def test_find_with_no_query_returns_usage(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    """A bare `/find` never dispatches to the service; usage text only."""
    called = False

    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        nonlocal called
        called = True
        return ConversationReply(kind="no_results")

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(text="/find"), config)

    assert not called
    assert len(surface.replies) == 1
    assert "/find" in surface.replies[0]


@pytest.mark.asyncio
async def test_find_recommendations_renders_numbered_list(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        return ConversationReply(kind="recommendations", candidates=(_event(),))

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(), config)

    assert len(surface.replies) == 1
    reply = surface.replies[0]
    assert "Live jazz" in reply
    assert "Sala Apolo" in reply
    assert "music" in reply
    # 1-indexed rendering.
    assert "1." in reply


@pytest.mark.asyncio
async def test_find_clarification_renders_question(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        return ConversationReply(kind="clarification", question="Which category?")

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(), config)

    (reply,) = surface.replies
    assert "Which category?" in reply


@pytest.mark.asyncio
async def test_find_detail_renders_summary(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        return ConversationReply(
            kind="detail", event=_event(), answer="Live jazz · 2026-08-01 · Sala Apolo · music"
        )

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(text="/find tell me about 1"), config)

    (reply,) = surface.replies
    assert "Live jazz" in reply
    assert "Sala Apolo" in reply


@pytest.mark.asyncio
async def test_find_no_results_renders_default_when_answer_absent(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        return ConversationReply(kind="no_results", answer=None)

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(), config)

    (reply,) = surface.replies
    # The shipped en-locale default message.
    assert "No matching events" in reply


@pytest.mark.asyncio
async def test_find_error_renders_error_type(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
) -> None:
    def fake_handle(*args: object, **kwargs: object) -> ConversationReply:
        return ConversationReply(kind="error", error_type="search_tool_failure")

    monkeypatch.setattr(commands, "handle_user_message", fake_handle)

    await handle_find(surface, conn, _msg(), config)

    (reply,) = surface.replies
    assert "search_tool_failure" in reply
