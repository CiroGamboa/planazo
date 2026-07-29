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
from typing import get_args

from planazo.identity.models import PreferenceReadResult, PreferenceRecord, ProfileField, UserRecord

_PROFILE_FIELD_COLUMNS = frozenset(get_args(ProfileField))


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
        age=row["age"],
        location=row["location"],
        language=row["language"],
        nationality=row["nationality"],
        pending_registration_field=row["pending_registration_field"],
    )


def _fetch_user(conn: sqlite3.Connection, user_id: int) -> UserRecord:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"expected a users row for id={user_id}, got none")
    return _user_from_row(row)


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


def set_pending_registration_field(
    conn: sqlite3.Connection, user_id: int, field: ProfileField | None
) -> UserRecord:
    """Set `user_id`'s registration pointer and return the refreshed row.

    Touches only `pending_registration_field` — no profile column changes.
    `field=None` clears the pointer, meaning no registration step is in
    flight (either never started, or the last run of the flow finished).
    """
    conn.execute(
        "UPDATE users SET pending_registration_field = ? WHERE id = ?",
        (field, user_id),
    )
    conn.commit()
    return _fetch_user(conn, user_id)


def record_registration_answer(
    conn: sqlite3.Connection,
    user_id: int,
    field: ProfileField,
    value: str | int,
    next_pending_field: ProfileField | None,
) -> UserRecord:
    """Write one registration answer and advance the pointer, atomically.

    `field` names the column `value` is written to; `next_pending_field`
    replaces `pending_registration_field` in the same `UPDATE`, so there is
    no state between "the answer landed" and "the pointer advanced" for a
    crash to land inside. `field` is checked against `ProfileField`'s known
    values before it is interpolated into the statement text — it names a
    column, which `sqlite3` cannot bind as a parameter, so this is the
    boundary check that keeps that interpolation confined to the five known
    columns.
    """
    if field not in _PROFILE_FIELD_COLUMNS:
        raise ValueError(f"{field!r} is not a known profile field")
    conn.execute(
        f"UPDATE users SET {field} = ?, pending_registration_field = ? WHERE id = ?",
        (value, next_pending_field, user_id),
    )
    conn.commit()
    return _fetch_user(conn, user_id)


def get_preferences(conn: sqlite3.Connection, user_id: int) -> PreferenceReadResult:
    """Return validated preference rows for `user_id`, ordered by ascending key.

    An unknown `user_id` is a successful empty read. A malformed persisted row
    yields one safe typed outcome and no partial rows, so composition can fail
    closed before model context is assembled.
    """
    rows = conn.execute(
        "SELECT * FROM preferences WHERE user_id = ? ORDER BY key", (user_id,)
    ).fetchall()
    try:
        preferences = tuple(
            PreferenceRecord(
                user_id=row["user_id"],
                key=row["key"],
                value=row["value"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return PreferenceReadResult(
            error_type="invalid_preference_data",
            message="Stored preference data could not be validated safely.",
        )
    return PreferenceReadResult(rows=preferences, message="")


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


def delete_preference(conn: sqlite3.Connection, user_id: int, key: str) -> bool:
    """Delete one preference for `user_id`, reporting whether a row went away.

    `True` means a `(user_id, key)` row existed and is gone; `False` means
    there was nothing to delete. An unknown `key` and a `user_id` with no
    `users` row are both `False` rather than a raise — unlike the INSERT in
    `set_preference`, a DELETE has no foreign key to violate, and the caller
    needs the two outcomes distinguishable to answer "removed" versus "no
    preference by that name" instead of reporting a silent success (rule 4).
    """
    cursor = conn.execute("DELETE FROM preferences WHERE user_id = ? AND key = ?", (user_id, key))
    conn.commit()
    return cursor.rowcount > 0
