"""Identity + preferences repository — narrow, connection-parameterized SQL.

Matches the two-tier pattern documented in ADR 0003: functions take an
explicit `sqlite3.Connection` and never open one themselves. Composing across
several primitives (a push-context assembly, a test against `":memory:"`)
runs against a single connection. `sqlite3.IntegrityError` propagates: no
LLM tool reaches these primitives, only our own composition code and tests
do, so a bad `user_id` is a caller bug and a loud failure is correct.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from planazo.identity.models import PreferenceRecord, UserRecord


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    """The row id of the INSERT `cursor` just executed."""
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("expected an INSERT cursor with a lastrowid, got None")
    return row_id


def _user_from_row(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        display_name=row["display_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def get_or_create_user(
    conn: sqlite3.Connection, telegram_user_id: str, display_name: str
) -> UserRecord:
    """Return the `users` row for `telegram_user_id`, creating it if absent.

    Idempotent by `telegram_user_id`: a second call with the same id returns
    the existing row (and its id), never a duplicate.
    """
    existing = conn.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if existing is not None:
        return _user_from_row(existing)

    created_at = datetime.now(UTC)
    record = UserRecord(
        telegram_user_id=telegram_user_id, display_name=display_name, created_at=created_at
    )
    cursor = conn.execute(
        "INSERT INTO users (telegram_user_id, display_name, created_at) VALUES (?, ?, ?)",
        (record.telegram_user_id, record.display_name, created_at.isoformat()),
    )
    conn.commit()
    return record.model_copy(update={"id": _last_row_id(cursor)})


def get_preferences(conn: sqlite3.Connection, user_id: int) -> list[PreferenceRecord]:
    """Return every preference row for `user_id`, by key.

    An unknown `user_id` yields `[]` — this is a read, not a constraint
    violation.
    """
    rows = conn.execute(
        "SELECT * FROM preferences WHERE user_id = ? ORDER BY key", (user_id,)
    ).fetchall()
    return [
        PreferenceRecord(
            user_id=row["user_id"],
            key=row["key"],
            value=row["value"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        for row in rows
    ]


def set_preference(
    conn: sqlite3.Connection, user_id: int, key: str, value: str
) -> PreferenceRecord:
    """Upsert one preference for `user_id` and return the stored row.

    `preferences` is keyed on `(user_id, key)`, so a plain second INSERT would
    raise; `ON CONFLICT ... DO UPDATE` replaces the value instead. A `user_id`
    with no `users` row raises `sqlite3.IntegrityError` — see this module's
    docstring on why that stays loud.
    """
    updated_at = datetime.now(UTC)
    record = PreferenceRecord(user_id=user_id, key=key, value=value, updated_at=updated_at)
    conn.execute(
        "INSERT INTO preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (record.user_id, record.key, record.value, updated_at.isoformat()),
    )
    conn.commit()
    return record
