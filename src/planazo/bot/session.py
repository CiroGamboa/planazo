"""The sender of a message, resolved to Planazo's own `users` row.

Session mapping is create-on-first-contact and keyed by `telegram_user_id`
(ADR 0011): the first message from an unseen sender inserts exactly one row,
and every later message from that sender resolves to it. There is no
registration step and no session table — the transport's stable user id is the
session.

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
