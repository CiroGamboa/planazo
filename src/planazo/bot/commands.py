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
it refuses to re-run an edited command. Every reply resolves at
`config.default_locale`; no per-user locale exists until #56.

Replies are plain text — see `planazo.bot.surface` — which is why a preference
value is echoed back verbatim rather than escaped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from planazo.bot.config import BotConfig, resolve
from planazo.bot.models import IncomingMessage
from planazo.bot.session import resolve_user
from planazo.catalog.models import Event
from planazo.conversation.models import ConversationReply
from planazo.conversation.repository import get_state
from planazo.conversation.service import handle_user_message
from planazo.identity import UserRecord, delete_preference, get_preferences, set_preference
from planazo.interfaces.surface import UserSurface

COMMANDS: Final[Mapping[str, str]] = {
    "/start": "cmd_start",
    "/help": "cmd_help",
    "/me": "cmd_me",
    "/prefs": "cmd_prefs",
    "/find": "cmd_find",
}


def _stored_id(user: UserRecord) -> int:
    """The `users.id` of a row that has been through the repository.

    `UserRecord.id` is `None` only for a record that has not been written yet,
    which `resolve_user` never returns. Raising rather than replying keeps the
    impossible case loud: it would mean the repository stopped returning the
    row it stored, not that the user typed something wrong.
    """
    if user.id is None:
        raise RuntimeError(f"resolved an unsaved users row for {user.telegram_user_id!r}")
    return user.id


def _command_list(config: BotConfig) -> str:
    """`COMMANDS`, rendered once for whichever command is listing them."""
    locale = config.default_locale
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


def _violations(config: BotConfig, error: ValidationError) -> str:
    """The refused constraints, in the words the user needs to read.

    `PreferenceRecord` is what bounds a key and a value and keeps both on one
    line, and its messages already name the field that failed, so they are
    surfaced instead of being flattened into a generic apology (AGENTS.md rule
    4). The pydantic `Value error, ` prefix is dropped because it labels which
    layer raised, which is not the user's problem.
    """
    locale = config.default_locale
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


def _list_preferences(conn: sqlite3.Connection, user_id: int, config: BotConfig) -> str:
    locale = config.default_locale
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
    conn: sqlite3.Connection, user_id: int, key: str, value: str, config: BotConfig
) -> str:
    locale = config.default_locale
    try:
        stored = set_preference(conn, user_id, key, value)
    except ValidationError as error:
        return resolve(config, "prefs_rejected", locale, reasons=_violations(config, error))
    return resolve(config, "prefs_saved", locale, key=stored.key, value=stored.value)


def _drop_preference(conn: sqlite3.Connection, user_id: int, key: str, config: BotConfig) -> str:
    locale = config.default_locale
    if delete_preference(conn, user_id, key):
        return resolve(config, "prefs_removed", locale, key=key)
    return resolve(config, "prefs_absent", locale, key=key)


async def handle_start(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Register the sender if they are new, then greet them and list the commands."""
    user = resolve_user(conn, message)
    await surface.reply(
        resolve(
            config,
            "start",
            config.default_locale,
            display_name=user.display_name,
            commands=_command_list(config),
        )
    )


async def handle_help(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """List the commands. The one command that reads and writes nothing."""
    await surface.reply(
        resolve(config, "help", config.default_locale, commands=_command_list(config))
    )


async def handle_me(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Report the sender's internal id, their Telegram handle, and how much is stored.

    A sender with no handle — Telegram does not require one — gets a phrase
    saying so, never a rendered `None`. A stored row that fails to validate on
    read (`PreferenceReadResult.error_type`) replaces the whole reply with
    `prefs_read_error` rather than reporting a count of zero, which would be a
    coerced "you have nothing stored" over a data-corruption failure (AGENTS.md
    rule 4).
    """
    locale = config.default_locale
    user_id = _stored_id(resolve_user(conn, message))
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
        resolve(config, "me", locale, user_id=user_id, handle=handle, count=len(result.rows))
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
    user_id = _stored_id(resolve_user(conn, message))

    if subcommand is None:
        await surface.reply(_list_preferences(conn, user_id, config))
    elif subcommand == "set" and len(parts) == 4:
        await surface.reply(_store_preference(conn, user_id, parts[2], parts[3], config))
    elif subcommand == "remove" and len(parts) > 2:
        await surface.reply(_drop_preference(conn, user_id, parts[2], config))
    else:
        await surface.reply(resolve(config, "prefs_usage", config.default_locale))


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


def _format_reply(config: BotConfig, reply: ConversationReply) -> str:
    """Project one `ConversationReply` onto the plain-text `.reply(...)` string.

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


async def handle_find(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Route `/find <query>` through the conversation service.

    Empty query (a bare `/find`) surfaces the usage text. Any other
    text — including a numeric answer to a prior clarification — is
    dispatched to `conversation.service.handle_user_message`, which
    owns the multi-turn logic. The reply is formatted by
    `_format_reply` against the shipped message catalog.
    """
    text = _strip_command_prefix(message.text)
    locale = config.default_locale
    if not text:
        await surface.reply(resolve(config, "find_usage", locale))
        return
    user_id = _stored_id(resolve_user(conn, message))
    reply = handle_user_message(conn, user_id, text)
    await surface.reply(_format_reply(config, reply))


async def handle_message(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Fallback handler for non-command text.

    Routes to `handle_user_message` only when the sender has an
    active `pending_clarification` — a plain "music" from a user
    with no state is still a no-op (the bot is silent on random
    text; users must invoke `/find` explicitly for a fresh query).
    This is what turns a clarification answer into a multi-turn
    continuation without hijacking every message the bot sees.
    """
    user_id = _stored_id(resolve_user(conn, message))
    state = get_state(conn, user_id)
    if state is None or state.pending_clarification is None:
        return
    reply = handle_user_message(conn, user_id, message.text.strip())
    await surface.reply(_format_reply(config, reply))
