"""The plain-text dispatch: registration, the register-first gate, or a turn.

`handle_plain_text` is what the tree's one `MessageHandler` wraps — and there
is exactly one, because PTB would race two handlers over the same non-command
text. It reads the sender's own `UserRecord` and picks one of three routes:
an in-flight registration answer continues unchanged through
`bot/registration.py`; an unregistered sender with nothing in flight is
pointed at `/register` with no further work; everyone else's text is one turn
of the multi-turn conversation, through `planazo.conversation`'s
`handle_user_message` composition root — the same one `/find` uses, which
owns the detail-lookup / more-results / clarification-answer / fresh-query
precedence itself (ADR 0016), so this module does not re-derive it. Like
every other module in `bot/` except `app.py`/`surface.py`, this one imports no
transport (ADR 0011).

A sender mid-clarification skips the register-first gate: `/find` does not
require a complete profile, so gating the *answer* to a question `/find`
already asked would wedge that conversation with no way to finish it.

The turn itself runs off the event-loop thread through
`commands.run_conversation_turn` (ADR 0011's threading contract) — with
`concurrent_updates=True` (ADR 0019) a multi-second turn on the event-loop
thread would stall every other sender's update, which is exactly the
cross-user independence the queue exists to provide. Any exception reaching
the provider is caught tightly around that one call and mapped to a
configured reply — the exception's own text never reaches the user.
"""

from __future__ import annotations

import sqlite3

from planazo.bot.commands import format_reply, run_conversation_turn
from planazo.bot.config import BotConfig, resolve_for
from planazo.bot.models import IncomingMessage
from planazo.bot.registration import handle_registration_answer
from planazo.bot.session import resolve_user, stored_id
from planazo.conversation.repository import get_state
from planazo.interfaces.surface import UserSurface


async def handle_plain_text(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Route one plain-text message to registration, a pending `/find`
    clarification, the register-first notice, or the agent loop, and reply
    exactly once.

    `resolve_user` runs first, unconditionally: every branch below reads the
    sender's own `UserRecord`. A sender mid-registration is handed to
    `handle_registration_answer` unchanged — this module does not duplicate
    or reinterpret that flow, and it wins over every other route because
    ADR 0013 gives the registration flow ownership of that sender's next
    message. A sender with a pending clarification has this message read as
    the answer to it, ahead of the register-first gate so an unregistered
    sender who ran `/find` can still finish the conversation it started. A
    sender who is neither mid-registration nor mid-clarification and has not
    completed their profile gets the register-first reply with no call into
    the agent loop at all. Otherwise, the sender's free text is run through
    the agent loop bound to their own stored `users.id`, never a hardcoded or
    cross-wired one.
    """
    user = resolve_user(conn, message)
    if user.is_mid_registration:
        await handle_registration_answer(surface, conn, message, config)
        return

    user_id = stored_id(user)
    state = get_state(conn, user_id)
    mid_clarification = state is not None and state.pending_clarification is not None

    if not mid_clarification and not user.profile_complete:
        await surface.reply(resolve_for(config, "chat_register_first", user))
        return

    try:
        reply = await run_conversation_turn(user_id, message.text.strip())
    except Exception:
        await surface.reply(resolve_for(config, "chat_provider_error", user))
        return

    await surface.reply(format_reply(config, reply))
