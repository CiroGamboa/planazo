"""Behaviour tests for the plain-text dispatch — real SQLite, recording surface.

Mirrors `tests/test_bot_registration.py`'s shape (a `RecordingSurface`, real
`data/bot.yaml`, no transport object in the path), but `conn` is backed by a
`tmp_path` file rather than `":memory:"`: `handle_plain_text`'s agent-loop
branch runs `event_agent.run_once` off the event-loop thread via
`asyncio.to_thread`, which opens its own connection to `db.DB_PATH` on a
different OS thread, and a `":memory:"` database is private per connection —
the two threads would never see each other's writes.

What is locked: a mid-registration sender still advances through
`bot/registration.py` unchanged, with the provider never called (routing is
undisturbed); an unregistered sender gets the register-first reply, again with
no provider call (AC5); a fully registered sender's free text reaches
`event_agent.run_once` and its final answer is relayed verbatim (AC1); two
distinct registered senders' free text binds to each one's own internal
`user_id`, never a shared or cross-wired one (AC3); and every non-answered
termination — truncated, max-steps, and a raised provider exception — gets its
own distinct, non-empty reply without raising out of `handle_plain_text` (AC4).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentlib.core import CHEAP, Result
from planazo.agents import event_agent, loop
from planazo.agents.loop import LoopResult
from planazo.bot.chat import handle_plain_text
from planazo.bot.config import BotConfig, load_config, resolve_for
from planazo.bot.models import IncomingMessage
from planazo.identity import (
    UserRecord,
    get_or_create_user,
    record_registration_answer,
    set_pending_registration_field,
)
from planazo.memory import facts
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


def make_result(**overrides: object) -> Result:
    """One canned `agentlib.core.Result`, mirroring `tests/test_event_agent.py`'s helper."""
    defaults: dict[str, object] = {
        "text": "ok",
        "model": CHEAP,
        "status": "completed",
        "stop_reason": None,
        "truncated": False,
        "input_tokens": 13,
        "cached_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.000009,
        "reasoning_summary": None,
    }
    defaults.update(overrides)
    return Result(**defaults)  # type: ignore[arg-type]


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
async def test_a_registered_senders_free_text_reaches_the_loop_and_relays_the_answer(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a fully registered sender's free text reaches `run_once`, and its
    one-turn answer is relayed to the user verbatim."""
    _register_complete(conn, "tg-1", "Dani V")
    monkeypatch.setattr(
        loop,
        "call",
        MagicMock(
            return_value=make_result(
                text="Techno tonight at Razzmatazz.", tool_calls=[], output_items=[]
            )
        ),
    )

    await handle_plain_text(surface, conn, make_message(text="anything fun tonight?"), config)

    assert surface.replies == ["Techno tonight at Razzmatazz."]


def _save_memory_turns(content: str) -> list[Result]:
    """Two canned turns: the first saves one fact via `save_memory`, the second answers."""
    arguments = {"cue": "favorite music", "content": content, "scope": "private"}
    tool_call = {"name": "save_memory", "arguments": arguments, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "save_memory",
        "arguments": json.dumps(arguments),
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="Noted.", tool_calls=[], output_items=[])
    return [turn_1, turn_2]


@pytest.mark.asyncio
async def test_two_senders_free_text_binds_to_each_ones_own_internal_user_id(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC3: `run_once` is bound to the sender's own internal id, never a
    hardcoded or cross-wired one — proven by a `save_memory` call from each of
    two distinct senders landing only under its own sender's private facts."""
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    user_a = _register_complete(conn, "tg-a", "Ada")
    user_b = _register_complete(conn, "tg-b", "Bo")
    assert user_a.id is not None
    assert user_b.id is not None

    monkeypatch.setattr(loop, "call", MagicMock(side_effect=_save_memory_turns("likes techno")))
    await handle_plain_text(
        surface, conn, make_message(telegram_user_id="tg-a", text="I like techno"), config
    )

    assert [f.content for f in facts.retrieve_facts(user_a.id, "favorite music", "private")] == [
        "likes techno"
    ]
    assert facts.retrieve_facts(user_b.id, "favorite music", "private") == []

    monkeypatch.setattr(loop, "call", MagicMock(side_effect=_save_memory_turns("likes jazz")))
    await handle_plain_text(
        surface, conn, make_message(telegram_user_id="tg-b", text="I like jazz"), config
    )

    assert [f.content for f in facts.retrieve_facts(user_b.id, "favorite music", "private")] == [
        "likes jazz"
    ]
    assert [f.content for f in facts.retrieve_facts(user_a.id, "favorite music", "private")] == [
        "likes techno"
    ]


_TRUNCATED_ANSWER = "Here's part of it…"


def _expected_reply(config: BotConfig, user: UserRecord, termination: str) -> str:
    if termination == "truncated":
        return resolve_for(config, "chat_truncated", user, answer=_TRUNCATED_ANSWER)
    if termination == "max_steps":
        return resolve_for(config, "chat_max_steps", user)
    return resolve_for(config, "chat_provider_error", user)


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["truncated", "max_steps", "provider_error"])
async def test_every_non_answered_termination_gets_its_own_distinct_reply(
    conn: sqlite3.Connection,
    surface: RecordingSurface,
    config: BotConfig,
    monkeypatch: pytest.MonkeyPatch,
    termination: str,
) -> None:
    """AC4: `truncated`, `max_steps`, and a raised provider exception each map
    to their own reply, none of them raises out of `handle_plain_text`, and —
    checked identically regardless of which case this run drives — the three
    possible replies never collide, so none of them could be mistaken for
    silently dropping the turn."""
    user = _register_complete(conn, "tg-1", "Dani V")
    if termination == "truncated":
        monkeypatch.setattr(
            event_agent,
            "run_loop",
            MagicMock(
                return_value=LoopResult(answer=_TRUNCATED_ANSWER, steps=3, stopped="truncated")
            ),
        )
    elif termination == "max_steps":
        monkeypatch.setattr(
            event_agent,
            "run_loop",
            MagicMock(return_value=LoopResult(answer=None, steps=8, stopped="max_steps")),
        )
    else:
        monkeypatch.setattr(
            loop, "call", MagicMock(side_effect=RuntimeError("the provider is unreachable"))
        )

    await handle_plain_text(surface, conn, make_message(text="anything fun tonight?"), config)

    (reply,) = surface.replies
    assert reply
    assert reply == _expected_reply(config, user, termination)

    every_reply = {
        kind: _expected_reply(config, user, kind)
        for kind in ("truncated", "max_steps", "provider_error")
    }
    assert len(set(every_reply.values())) == 3
