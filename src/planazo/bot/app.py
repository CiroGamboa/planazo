"""The transport shell — `Application` wiring, the update adapter, `main()`.

The edge of the package, and with `surface.py` the only module here that
imports `telegram`. It owns three things: building the `Application` with one
`CommandHandler` per command, converting a PTB `Update` into the validated
`IncomingMessage` the command layer consumes, and long polling.

`adapter_for` is the seam. It turns any PTB-free command coroutine into a PTB
callback, so a new command is registered by naming its coroutine rather than
by writing transport code again. The same seam wraps the one `MessageHandler`
in the tree: the plain-text continuation for an in-flight registration
answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Final

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ExtBot,
    JobQueue,
    MessageHandler,
    filters,
)

from planazo.bot.commands import handle_help, handle_me, handle_prefs, handle_start
from planazo.bot.config import BotConfig, load_config, resolve
from planazo.bot.models import IncomingMessage
from planazo.bot.registration import handle_register, handle_registration_answer
from planazo.bot.surface import surface_for
from planazo.config import read_bot_token
from planazo.interfaces.surface import UserSurface
from planazo.storage import db

# `Any`: PTB's own generic defaults for the user-, chat-, and bot-data slots,
# which this bot does not use. Bare `Application` fails `mypy --strict` with
# "Missing type arguments for generic type", so all six parameters are spelled
# out. `JobQueue` imports from the base install; the `[job-queue]` extra is not
# needed, and nothing here reads `Application.job_queue` — that property is the
# only thing that warns about its absence, so leaving it untouched is what
# keeps the build quiet.
type BotApplication = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    dict[Any, Any],
    dict[Any, Any],
    dict[Any, Any],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]

type BotCommand = Callable[
    [UserSurface, sqlite3.Connection, IncomingMessage, BotConfig], Awaitable[None]
]

# `Any`: the send and yield types of a coroutine nobody drives by hand.
# `CommandHandler` types its callback as returning `Coroutine`, not the wider
# `Awaitable`, so this alias has to match it exactly to register.
type UpdateCallback = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]

_HANDLERS: Final[Mapping[str, BotCommand]] = {
    "start": handle_start,
    "help": handle_help,
    "me": handle_me,
    "prefs": handle_prefs,
    "register": handle_register,
}


def adapter_for(command: BotCommand, config: BotConfig) -> UpdateCallback:
    """Wrap a PTB-free command coroutine into a PTB handler callback.

    The adapter is the whole transport contract, in one order:

    1. Read `update.effective_message`, never `update.message`. A
       `CommandHandler`'s default filter is `filters.UpdateType.MESSAGES`,
       which is `MESSAGE | EDITED_MESSAGE`, and an edited command arrives with
       `update.message is None`. Reading `update.message` would raise into
       PTB's error logger and leave the user with silence.
    2. Ignore an update carrying no user, no message, or no message text —
       a typed branch with no reply, not a crash.
    3. Bind the reply channel from `context.bot` and the message's chat.
    4. Refuse an edited command. Re-running one replays an *old* command
       against *newer* state, which is a silent wrong outcome on persisted
       data: `set city Barcelona`, `remove city`, `set city Madrid`, then an
       edit of the second message would delete Madrid and answer "removed".
       The refusal happens after the surface exists, so the user is told, and
       before the database is opened, so "writes nothing" is a property of the
       control flow rather than an assertion about it.
    5. Validate the update into an `IncomingMessage` (AGENTS.md rule 1). The
       text is passed through unmodified — not pre-split, and not read from
       `context.args`, which drops the line breaks `/prefs set` must be able
       to reject.
    6. Open one connection, run the command, close it in a `finally`. With
       PTB's default `concurrent_updates` of 1, updates are handled one at a
       time, so a per-invocation synchronous connection never crosses threads.
    """

    async def adapter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None or message.text is None:
            return

        surface = surface_for(context.bot, message.chat_id)

        if update.edited_message is not None:
            await surface.reply(resolve(config, "edited_command", config.default_locale))
            return

        incoming = IncomingMessage(
            telegram_user_id=str(user.id),
            display_name=user.full_name,
            telegram_handle=user.username,
            text=message.text,
        )

        conn = db.connect()
        try:
            await command(surface, conn, incoming, config)
        finally:
            conn.close()

    return adapter


def build_application(token: str, config: BotConfig) -> BotApplication:
    """Build the `Application` with one `CommandHandler` per command, plus one
    `MessageHandler` for a plain-text registration answer.

    `filters.TEXT & ~filters.COMMAND` is what keeps the two kinds of update
    from shadowing each other: PTB's `filters.COMMAND` matches any update
    carrying a `BOT_COMMAND` entity regardless of whether a `CommandHandler`
    claims it, so excluding it here keeps a command update routing to its own
    `CommandHandler` only, never to this one.
    """
    application: BotApplication = ApplicationBuilder().token(token).build()
    for name, command in _HANDLERS.items():
        application.add_handler(CommandHandler(name, adapter_for(command, config)))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, adapter_for(handle_registration_answer, config)
        )
    )
    return application


def main() -> int:
    """Load and validate the bot config, then long-poll until interrupted.

    `load_config()` runs first, before the token check and before any
    Telegram connection opens: a malformed or incomplete `data/bot.yaml`
    raises `ValidationError` uncaught right here (AGENTS.md rule 1, rule 4).
    Long polling rather than a webhook: no public HTTPS endpoint, certificate,
    or deployment target is needed, which is what lets the bot run from a
    laptop and a `.env` (ADR 0011).
    """
    config = load_config()
    token = read_bot_token()
    if token is None:
        return 1
    build_application(token, config).run_polling()
    return 0
