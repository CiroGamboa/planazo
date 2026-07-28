"""The four commands, and every word the bot says.

Each command is a coroutine with the same signature — `(surface: UserSurface,
conn: sqlite3.Connection, message: IncomingMessage) -> None` — and this module
imports no transport (ADR 0011). What follows is therefore CRUD against real
SQLite that any surface can drive: the transport shell converts one update
into an `IncomingMessage`, binds a reply channel, and calls one of these.

`COMMANDS` and `MESSAGES` are the complete user-facing copy. No reply text is
spelled anywhere else in the package, so the whole set moves in one piece when
it is externalized. `COMMANDS` is also the single command list that `/start`
and `/help` both render, so the two cannot drift.

Replies are plain text — see `planazo.bot.surface` — which is why a preference
value is echoed back verbatim rather than escaped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from planazo.bot.models import IncomingMessage
from planazo.bot.session import resolve_user
from planazo.identity import UserRecord, delete_preference, get_preferences, set_preference
from planazo.interfaces.surface import UserSurface

COMMANDS: Final[Mapping[str, str]] = {
    "/start": "sign you up and show this list",
    "/help": "show this list",
    "/me": "show your account and how many preferences you have stored",
    "/prefs": "view, set, or remove your preferences",
}

MESSAGES: Final[Mapping[str, str]] = {
    "command_line": "{command} — {description}",
    "start": "Hi {display_name}! You are all set. Here is what I can do:\n\n{commands}",
    "help": "Here is what I can do:\n\n{commands}",
    "me": "Your Planazo id: {user_id}\nTelegram: {handle}\nPreferences stored: {count}",
    "me_handle": "@{handle}",
    "me_no_handle": "no handle set",
    "prefs_usage": (
        "Use one of:\n"
        "/prefs — list your preferences\n"
        "/prefs set <key> <value> — store or replace one\n"
        "/prefs remove <key> — delete one"
    ),
    "prefs_empty": "You have no preferences stored.\n\n{usage}",
    "prefs_list": "Your preferences:\n\n{lines}",
    "prefs_line": "{key}: {value}",
    "prefs_saved": "Saved {key}: {value}",
    "prefs_removed": "Removed {key}.",
    "prefs_absent": "You have no preference named {key}.",
    "prefs_rejected": "I did not save that:\n{reasons}",
    "prefs_violation": "{field}: {problem}",
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


def _command_list() -> str:
    """`COMMANDS`, rendered once for whichever command is listing them."""
    return "\n".join(
        MESSAGES["command_line"].format(command=command, description=description)
        for command, description in COMMANDS.items()
    )


def _violations(error: ValidationError) -> str:
    """The refused constraints, in the words the user needs to read.

    `PreferenceRecord` is what bounds a key and a value and keeps both on one
    line, and its messages already name the field that failed, so they are
    surfaced instead of being flattened into a generic apology (AGENTS.md rule
    4). The pydantic `Value error, ` prefix is dropped because it labels which
    layer raised, which is not the user's problem.
    """
    return "\n".join(
        MESSAGES["prefs_violation"].format(
            field=".".join(str(part) for part in item["loc"]),
            problem=item["msg"].removeprefix("Value error, "),
        )
        for item in error.errors()
    )


def _list_preferences(conn: sqlite3.Connection, user_id: int) -> str:
    stored = get_preferences(conn, user_id)
    if not stored:
        return MESSAGES["prefs_empty"].format(usage=MESSAGES["prefs_usage"])
    lines = "\n".join(MESSAGES["prefs_line"].format(key=row.key, value=row.value) for row in stored)
    return MESSAGES["prefs_list"].format(lines=lines)


def _store_preference(conn: sqlite3.Connection, user_id: int, key: str, value: str) -> str:
    try:
        stored = set_preference(conn, user_id, key, value)
    except ValidationError as error:
        return MESSAGES["prefs_rejected"].format(reasons=_violations(error))
    return MESSAGES["prefs_saved"].format(key=stored.key, value=stored.value)


def _drop_preference(conn: sqlite3.Connection, user_id: int, key: str) -> str:
    if delete_preference(conn, user_id, key):
        return MESSAGES["prefs_removed"].format(key=key)
    return MESSAGES["prefs_absent"].format(key=key)


async def handle_start(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage
) -> None:
    """Register the sender if they are new, then greet them and list the commands."""
    user = resolve_user(conn, message)
    await surface.reply(
        MESSAGES["start"].format(display_name=user.display_name, commands=_command_list())
    )


async def handle_help(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage
) -> None:
    """List the commands. The one command that reads and writes nothing."""
    await surface.reply(MESSAGES["help"].format(commands=_command_list()))


async def handle_me(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage
) -> None:
    """Report the sender's internal id, their Telegram handle, and how much is stored.

    A sender with no handle — Telegram does not require one — gets a phrase
    saying so, never a rendered `None`.
    """
    user_id = _stored_id(resolve_user(conn, message))
    handle = (
        MESSAGES["me_no_handle"]
        if message.telegram_handle is None
        else MESSAGES["me_handle"].format(handle=message.telegram_handle)
    )
    await surface.reply(
        MESSAGES["me"].format(
            user_id=user_id,
            handle=handle,
            count=len(get_preferences(conn, user_id)),
        )
    )


async def handle_prefs(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage
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
    refused by a constraint, or the usage text for anything unparseable.
    """
    parts = message.text.split(None, 3)
    subcommand = parts[1] if len(parts) > 1 else None
    user_id = _stored_id(resolve_user(conn, message))

    if subcommand is None:
        await surface.reply(_list_preferences(conn, user_id))
    elif subcommand == "set" and len(parts) == 4:
        await surface.reply(_store_preference(conn, user_id, parts[2], parts[3]))
    elif subcommand == "remove" and len(parts) > 2:
        await surface.reply(_drop_preference(conn, user_id, parts[2]))
    else:
        await surface.reply(MESSAGES["prefs_usage"])
