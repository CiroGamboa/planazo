"""Conversation repository — connection-parameterized SQL for `conversation_state`.

Two-tier pattern per ADR 0003: functions take an explicit
`sqlite3.Connection` and never open one themselves. Composing across
several primitives (a test against `":memory:"`, the service
composition root running against `db.connect()`) runs against a
single connection.

The primitive commits on success. `sqlite3.IntegrityError` propagates:
the FK on `user_id` is a boundary lock, and a violation is a caller
bug (an unseeded users row) that the service should see rather than
swallow.

`get_state` returns `None` for an absent user_id — a legitimate
successful empty read, matching the shape of other repository
primitives in the tree (e.g. `catalog.list_extraction_runs` returns
an empty list for a user with no runs).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from planazo.conversation.models import ConversationState, PendingClarification


def _state_from_row(row: sqlite3.Row) -> ConversationState:
    """Rehydrate one `ConversationState` from a `sqlite3.Row`.

    `pending_clarification` is stored as JSON via
    `PendingClarification.model_dump_json()`; on read, an absent
    (NULL) column stays `None`, and a populated column round-trips
    through `PendingClarification.model_validate_json()`. A malformed
    JSON blob is a caller bug (raw-SQL diagnostic write bypassed the
    aggregate) — the ValidationError propagates and the service's
    best-effort seam decides whether to swallow.
    """
    pending_raw: str | None = row["pending_clarification"]
    pending = (
        PendingClarification.model_validate_json(pending_raw) if pending_raw is not None else None
    )
    return ConversationState(
        user_id=row["user_id"],
        pending_clarification=pending,
        last_recommendation_run_id=row["last_recommendation_run_id"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def get_state(conn: sqlite3.Connection, user_id: int) -> ConversationState | None:
    """Return the `conversation_state` row for `user_id`, or `None` if absent.

    An unknown `user_id` is a successful empty read — the service's
    "fresh query" path fires for a user who has never sent a message
    before. A malformed JSON blob in `pending_clarification` raises a
    Pydantic `ValidationError` (see `_state_from_row`), which the
    service's best-effort composition root can catch or let propagate.
    """
    row = conn.execute(
        "SELECT user_id, pending_clarification, last_recommendation_run_id, updated_at"
        " FROM conversation_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return _state_from_row(row)


def upsert_state(conn: sqlite3.Connection, state: ConversationState) -> None:
    """Insert or replace the `conversation_state` row for `state.user_id`.

    Per-user single-row invariant is enforced by the `PRIMARY KEY` on
    `user_id`: a second INSERT with the same `user_id` would raise; the
    `ON CONFLICT ... DO UPDATE` handles that by replacing the two
    scratchpad columns plus `updated_at`. `pending_clarification` is
    serialised via `PendingClarification.model_dump_json()` when set,
    NULL otherwise — matching the migration's TEXT column.

    Commits on success. A `user_id` with no `users` row raises
    `sqlite3.IntegrityError` — the FK is a boundary lock and the
    service is the one caller that should see a violation.
    """
    pending_json: str | None = (
        state.pending_clarification.model_dump_json()
        if state.pending_clarification is not None
        else None
    )
    updated_at = state.updated_at.isoformat()
    conn.execute(
        "INSERT INTO conversation_state"
        " (user_id, pending_clarification, last_recommendation_run_id, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET"
        " pending_clarification = excluded.pending_clarification,"
        " last_recommendation_run_id = excluded.last_recommendation_run_id,"
        " updated_at = excluded.updated_at",
        (state.user_id, pending_json, state.last_recommendation_run_id, updated_at),
    )
    conn.commit()


def now_utc() -> datetime:
    """Return the current UTC instant.

    Wrapped in a helper so the service composition root can construct
    a `ConversationState` with a deterministic timestamp when needed
    (tests, monkeypatched clocks).
    """
    return datetime.now(UTC)
