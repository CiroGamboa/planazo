"""The sender of a message, resolved to Planazo's own `users` row.

Session mapping is create-on-first-contact and keyed by `telegram_user_id`
(ADR 0011): the first message from an unseen sender inserts exactly one row,
and every later message from that sender resolves to it. There is still no
session table — the transport's stable user id is the session — but a
registration step now exists, tracked on that same `users` row via
`UserRecord.pending_registration_field` (`bot/registration.py`,
`docs/adr/0013-registration-conversation-state.md`).

This module imports no transport, so a second surface resolves its senders
through the same function by projecting them into an `IncomingMessage` first.
"""

from __future__ import annotations

import sqlite3

from planazo.bot.models import IncomingMessage
from planazo.identity import UserRecord, get_or_create_user


def resolve_user(conn: sqlite3.Connection, message: IncomingMessage) -> UserRecord:
    """Return the sender's `users` row, creating it on first contact.

    Get-or-create, not upsert: a sender who has since renamed themselves on
    Telegram keeps the `display_name` stored on their first contact, because
    that is what `get_or_create_user` guarantees and what makes repeated calls
    idempotent by `telegram_user_id`.
    """
    return get_or_create_user(conn, message.telegram_user_id, message.display_name)


def stored_id(user: UserRecord) -> int:
    """The `users.id` of a row that has been through the repository.

    `UserRecord.id` is `None` only for a record that has not been written yet,
    which `resolve_user` never returns. Raising rather than replying keeps the
    impossible case loud: it would mean the repository stopped returning the
    row it stored, not that the user typed something wrong.
    """
    if user.id is None:
        raise RuntimeError(f"resolved an unsaved users row for {user.telegram_user_id!r}")
    return user.id
