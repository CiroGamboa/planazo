"""The four commands — CRUD against SQLite; no reply text lives here.

Each command is a coroutine with the same signature — `(surface: UserSurface,
conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig) ->
None` — and this module imports no transport (ADR 0011). What follows is
therefore CRUD against real SQLite that any surface can drive: the transport
shell converts one update into an `IncomingMessage`, binds a reply channel,
and calls one of these.

`COMMANDS` maps each command to a message id in `data/bot.yaml` — a
structural identifier, not copy, the same status `_HANDLERS`'s keys already
have in `app.py` — so `/start` and `/help` can render the same list without
drifting. Every reply is produced by `planazo.bot.config.resolve` against
`config`, which is also what the transport shell calls on its own behalf when
it refuses to re-run an edited command. Every reply resolves through the
sender's stored `UserRecord.language`, falling back to `config.default_locale`
when unset — computed once per handler and threaded down as `locale`.

Replies are plain text — see `planazo.bot.surface` — which is why a preference
value is echoed back verbatim rather than escaped.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from planazo.bot.config import BotConfig, resolve
from planazo.bot.models import IncomingMessage
from planazo.bot.session import resolve_user, stored_id
from planazo.catalog.models import Event
from planazo.conversation.models import ConversationReply
from planazo.conversation.service import handle_user_message
from planazo.identity import delete_preference, get_preferences, set_preference
from planazo.interfaces.surface import UserSurface
from planazo.storage import db

COMMANDS: Final[Mapping[str, str]] = {
    "/start": "cmd_start",
    "/help": "cmd_help",
    "/me": "cmd_me",
    "/prefs": "cmd_prefs",
    "/register": "cmd_register",
    "/find": "cmd_find",
}


def _command_list(config: BotConfig, locale: str) -> str:
    """`COMMANDS`, rendered once for whichever command is listing them."""
    return "\n".join(
        resolve(
            config,
            "command_line",
            locale,
            command=command,
            description=resolve(config, description_id, locale),
        )
        for command, description_id in COMMANDS.items()
    )


def _violations(config: BotConfig, error: ValidationError, locale: str) -> str:
    """The refused constraints, in the words the user needs to read.

    `PreferenceRecord` is what bounds a key and a value and keeps both on one
    line, and its messages already name the field that failed, so they are
    surfaced instead of being flattened into a generic apology (AGENTS.md rule
    4). The pydantic `Value error, ` prefix is dropped because it labels which
    layer raised, which is not the user's problem.
    """
    return "\n".join(
        resolve(
            config,
            "prefs_violation",
            locale,
            field=".".join(str(part) for part in item["loc"]),
            problem=item["msg"].removeprefix("Value error, "),
        )
        for item in error.errors()
    )


def _list_preferences(
    conn: sqlite3.Connection, user_id: int, config: BotConfig, locale: str
) -> str:
    result = get_preferences(conn, user_id)
    if result.error_type is not None:
        return resolve(config, "prefs_read_error", locale)
    if not result.rows:
        return resolve(config, "prefs_empty", locale, usage=resolve(config, "prefs_usage", locale))
    lines = "\n".join(
        resolve(config, "prefs_line", locale, key=row.key, value=row.value) for row in result.rows
    )
    return resolve(config, "prefs_list", locale, lines=lines)


def _store_preference(
    conn: sqlite3.Connection, user_id: int, key: str, value: str, config: BotConfig, locale: str
) -> str:
    try:
        stored = set_preference(conn, user_id, key, value)
    except ValidationError as error:
        return resolve(config, "prefs_rejected", locale, reasons=_violations(config, error, locale))
    return resolve(config, "prefs_saved", locale, key=stored.key, value=stored.value)


def _drop_preference(
    conn: sqlite3.Connection, user_id: int, key: str, config: BotConfig, locale: str
) -> str:
    if delete_preference(conn, user_id, key):
        return resolve(config, "prefs_removed", locale, key=key)
    return resolve(config, "prefs_absent", locale, key=key)


async def handle_start(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Register the sender if they are new, then greet them and list the commands."""
    user = resolve_user(conn, message)
    locale = user.language or config.default_locale
    await surface.reply(
        resolve(
            config,
            "start",
            locale,
            display_name=user.display_name,
            commands=_command_list(config, locale),
        )
    )


async def handle_help(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """List the commands.

    Resolves the sender (create-on-first-contact) purely to read their stored
    locale — this handler still writes nothing and reads no other field.
    """
    user = resolve_user(conn, message)
    locale = user.language or config.default_locale
    await surface.reply(resolve(config, "help", locale, commands=_command_list(config, locale)))


async def handle_me(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Report the sender's stored profile, or point them at `/register`.

    `locale` is resolved once, from the sender's own stored `language`,
    before any of the three outcomes below — including `me_not_registered` —
    so a sender who has answered the language step but not finished
    registration still gets that outcome in their own language rather than
    `config.default_locale`.

    Three mutually exclusive outcomes, checked in this order
    (`docs/adr/0018-registration-conversation-state.md`):
    1. `profile_complete` is `False` — the whole reply is `me_not_registered`;
       no preference read happens at all, regardless of whether the sender's
       preference data is fine or corrupt. This is an absolute gate, not a
       best-effort one.
    2. Preferences fail to read (`PreferenceReadResult.error_type` set) — the
       whole reply is `prefs_read_error`, exactly as before.
    3. Otherwise — the full profile (`display_name`, `age`, `location`,
       `language`, `nationality`) plus `user_id`, `handle`, and the preference
       count. A sender with no handle — Telegram does not require one — gets
       a phrase saying so, never a rendered `None`.
    """
    user = resolve_user(conn, message)
    locale = user.language or config.default_locale
    if not user.profile_complete:
        await surface.reply(resolve(config, "me_not_registered", locale))
        return

    user_id = stored_id(user)
    result = get_preferences(conn, user_id)
    if result.error_type is not None:
        await surface.reply(resolve(config, "prefs_read_error", locale))
        return
    handle = (
        resolve(config, "me_no_handle", locale)
        if message.telegram_handle is None
        else resolve(config, "me_handle", locale, handle=message.telegram_handle)
    )
    preferences = get_preferences(conn, user_id)
    if preferences.error_type is not None:
        await surface.reply(
            resolve(
                config,
                "me_preferences_unavailable",
                locale,
                user_id=user_id,
                handle=handle,
            )
        )
        return
    await surface.reply(
        resolve(
            config,
            "me",
            locale,
            user_id=user_id,
            handle=handle,
            display_name=user.display_name,
            age=user.age,
            location=user.location,
            language=user.language,
            nationality=user.nationality,
            count=len(result.rows),
        )
    )


async def handle_prefs(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """List, store, or delete the sender's preferences.

    The arguments are parsed out of the raw message text with
    `split(None, 3)`, which keeps the value's own whitespace — including a line
    break — so a value that `PreferenceRecord` refuses reaches it intact and
    comes back as a refusal the user can read, rather than being silently
    reshaped into something acceptable. Parsing is positional, so the
    `/prefs@botname` form Telegram delivers in group chats is read exactly like
    the bare one.

    Every outcome is its own reply: stored, removed, no such preference,
    refused by a constraint, unreadable on a corrupt stored row, or the usage
    text for anything unparseable.
    """
    parts = message.text.split(None, 3)
    subcommand = parts[1] if len(parts) > 1 else None
    user = resolve_user(conn, message)
    locale = user.language or config.default_locale
    user_id = stored_id(user)

    if subcommand is None:
        await surface.reply(_list_preferences(conn, user_id, config, locale))
    elif subcommand == "set" and len(parts) == 4:
        await surface.reply(_store_preference(conn, user_id, parts[2], parts[3], config, locale))
    elif subcommand == "remove" and len(parts) > 2:
        await surface.reply(_drop_preference(conn, user_id, parts[2], config, locale))
    else:
        await surface.reply(resolve(config, "prefs_usage", locale))


def _strip_command_prefix(text: str) -> str:
    """Return everything after the `/find` (or `/find@botname`) command token.

    Group chats deliver `/find@planazo_bot query`; the suffix belongs
    to the command token and must not shift the arguments behind it.
    Matches `handle_prefs`'s positional parsing shape.
    """
    parts = text.split(None, 1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _format_price(config: BotConfig, price_cents: int) -> str:
    """Render a `price_cents` value as a short display string via `resolve`."""
    locale = config.default_locale
    if price_cents == 0:
        return resolve(config, "find_recommendation_price_free", locale)
    amount = f"{price_cents / 100:.2f}"
    return resolve(config, "find_recommendation_price_eur", locale, amount=amount)


def _format_recommendation_line(config: BotConfig, index_one_based: int, event: Event) -> str:
    """Render one Recommender candidate onto a numbered `find_recommendation_line`."""
    locale = config.default_locale
    venue = (
        event.venue_name
        if event.venue_name is not None
        else resolve(config, "find_recommendation_venue_missing", locale)
    )
    when = event.start_utc.strftime("%Y-%m-%d %H:%M UTC")
    return resolve(
        config,
        "find_recommendation_line",
        locale,
        n=index_one_based,
        title=event.title,
        when=when,
        venue=venue,
        category=event.category,
        price=_format_price(config, event.price_cents),
    )


def format_reply(config: BotConfig, reply: ConversationReply) -> str:
    """Project one `ConversationReply` onto the plain-text `.reply(...)` string.

    Public because `bot/chat.py` renders the same `ConversationReply` when a
    plain-text message answers a pending clarification — the tree has exactly
    one plain-text `MessageHandler`, and it lives there.

    The five `kind` branches map to the `find_*` message-ids in
    `data/bot.yaml`. `no_results` reads `answer` when present and
    falls back to the shipped default so the user always sees a
    concrete explanation.
    """
    locale = config.default_locale
    if reply.kind == "recommendations":
        lines = "\n".join(
            _format_recommendation_line(config, position, event)
            for position, event in enumerate(reply.candidates, start=1)
        )
        return resolve(config, "find_recommendations", locale, lines=lines)
    if reply.kind == "clarification":
        question = reply.question if reply.question is not None else ""
        return resolve(config, "find_clarification", locale, question=question)
    if reply.kind == "detail":
        # The detail card is the `answer` field the service already
        # rendered (or a fallback via the Event's title).
        summary = reply.answer if reply.answer else (reply.event.title if reply.event else "")
        # Detail lookup does not track a stable 1-indexed n through
        # the service surface — the surface renders the event only,
        # so we drop the number here. `n=""` is intentional.
        return resolve(config, "find_detail", locale, n="", summary=summary)
    if reply.kind == "no_results":
        message = (
            reply.answer if reply.answer else resolve(config, "find_no_results_default", locale)
        )
        return resolve(config, "find_no_results", locale, message=message)
    error_type = reply.error_type if reply.error_type is not None else "unknown_error"
    return resolve(config, "find_error", locale, error_type=error_type)


async def run_conversation_turn(user_id: int, text: str) -> ConversationReply:
    """Run one `handle_user_message` turn off the event-loop thread.

    The turn reaches the LLM provider and is synchronous, so running it on the
    event-loop thread would stall every other sender's update — ADR 0011's
    threading contract, load-bearing now that ADR 0019 sets
    `concurrent_updates=True` precisely so different senders run concurrently.

    It opens its own connection *inside* the worker thread rather than reusing
    the adapter's: `sqlite3` binds a connection to the thread that created it
    (`check_same_thread` defaults to `True`), so handing the adapter's
    connection across the thread hop raises `ProgrammingError`. This mirrors
    how the agent loop's own tools already open per-call connections.
    """

    def turn() -> ConversationReply:
        conn = db.connect()
        try:
            return handle_user_message(conn, user_id, text)
        finally:
            conn.close()

    return await asyncio.to_thread(turn)


async def handle_find(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Route `/find <query>` through the conversation service.

    Empty query (a bare `/find`) surfaces the usage text. Any other
    text — including a numeric answer to a prior clarification — is
    dispatched to `conversation.service.handle_user_message`, which
    owns the multi-turn logic. The reply is formatted by
    `format_reply` against the shipped message catalog.

    The turn runs off the event-loop thread via `run_conversation_turn` (see
    its docstring for why, and why it opens its own connection). A provider
    failure is caught here and mapped to a configured reply rather than
    surfacing the exception text.
    """
    text = _strip_command_prefix(message.text)
    locale = config.default_locale
    if not text:
        await surface.reply(resolve(config, "find_usage", locale))
        return
    user_id = stored_id(resolve_user(conn, message))
    try:
        reply = await run_conversation_turn(user_id, text)
    except Exception:
        await surface.reply(resolve(config, "chat_provider_error", locale))
        return
    await surface.reply(format_reply(config, reply))
