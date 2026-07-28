"""The three-way plain-text dispatch: registration, gate, or the agent loop.

`handle_plain_text` is what the tree's one `MessageHandler` now wraps. It
reads the sender's own `UserRecord` and picks exactly one of three routes,
in order: an in-flight registration answer continues unchanged through
`bot/registration.py`; an unregistered sender is pointed at `/register`
with no further work; a fully registered sender's free text reaches the
composition root (`planazo.agents.event_agent.run_once`) — the one and only
place in this package that ever crosses into the LLM provider. Like every
other module in `bot/` except `app.py`/`surface.py`, this one imports no
transport (ADR 0011).

`run_once` is synchronous and blocking, so it runs off the event-loop thread
via `asyncio.to_thread` (ADR 0011's threading contract) — otherwise a single
multi-second run would stall every other user's update, not just this one.
Its outcome is mapped to exactly one reply: an `"answered"` or
`"preference_read_error"` stop relays `result.answer` verbatim, `"truncated"`
wraps the partial answer, `"max_steps"` gets an answer-free reply, and any
exception raised while reaching the provider is caught tightly around that
one call and mapped to its own reply — the exception's own text never
reaches the user.
"""

from __future__ import annotations

import asyncio
import sqlite3

from planazo.agents import event_agent
from planazo.bot.config import BotConfig, resolve_for
from planazo.bot.models import IncomingMessage
from planazo.bot.registration import handle_registration_answer
from planazo.bot.session import resolve_user, stored_id
from planazo.interfaces.surface import UserSurface


async def handle_plain_text(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Route one plain-text message to registration, the register-first
    notice, or the agent loop, and reply exactly once.

    `resolve_user` runs first, unconditionally: every branch below reads the
    sender's own `UserRecord`. A sender mid-registration is handed to
    `handle_registration_answer` unchanged — this module does not duplicate
    or reinterpret that flow. A sender who is not mid-registration and has
    not completed their profile gets the register-first reply with no call
    into the agent loop at all. Otherwise, the sender's free text is run
    through the agent loop bound to their own stored `users.id`, never a
    hardcoded or cross-wired one.
    """
    user = resolve_user(conn, message)
    if user.is_mid_registration:
        await handle_registration_answer(surface, conn, message, config)
        return
    if not user.profile_complete:
        await surface.reply(resolve_for(config, "chat_register_first", user))
        return

    user_id = stored_id(user)
    try:
        result = await asyncio.to_thread(event_agent.run_once, message.text, user_id=user_id)
    except Exception:
        await surface.reply(resolve_for(config, "chat_provider_error", user))
        return

    if result.stopped == "max_steps":
        await surface.reply(resolve_for(config, "chat_max_steps", user))
        return
    if result.stopped == "truncated":
        if result.answer is None:
            raise RuntimeError(
                f"run_once returned stopped='truncated' with no partial answer for "
                f"user_id={user_id!r}"
            )
        await surface.reply(resolve_for(config, "chat_truncated", user, answer=result.answer))
        return

    if result.answer is None:
        raise RuntimeError(
            f"run_once returned stopped={result.stopped!r} with no answer for user_id={user_id!r}"
        )
    await surface.reply(result.answer)
