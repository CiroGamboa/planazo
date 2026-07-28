"""Scheduler repository — narrow, connection-parameterized SQL for `scan_state`.

Matches the two-tier pattern documented in ADR 0003: functions take an
explicit `sqlite3.Connection` and never open one themselves. Composing across
several primitives (a tick, a test against `":memory:"`) runs against a
single connection. `sqlite3.IntegrityError` propagates: no LLM tool reaches
these primitives, only the tick service and tests do, so a bad `source_url`
is a caller bug and a loud failure is correct.

`bootstrap_system_user` is a thin idempotent wrapper around
`identity.repository.get_or_create_user` that seeds the fixed
`telegram_user_id="system"` row every scheduler tick will attribute
`extract_once` calls to. It lives here rather than in `identity/` because
the "there is a system user" concept belongs to the scheduler bounded
context; `identity/` provides the primitive, this module composes it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from planazo.identity.models import UserRecord
from planazo.identity.repository import get_or_create_user
from planazo.scheduler.models import ScanState

SYSTEM_USER_TELEGRAM_ID = "system"
"""The fixed `users.telegram_user_id` value for the scheduler's attribution row.

Not an env var: rule-10 discipline. The scheduler always attributes
`extract_once` calls to the same synthetic user, and having the identifier
live as a constant here keeps every caller (tick service, tests,
follow-up tooling) pointed at the same row.
"""

SYSTEM_USER_DISPLAY_NAME = "Scheduled Scanner"


def _scan_state_from_row(row: sqlite3.Row) -> ScanState:
    last_scanned = row["last_scanned_at"]
    last_success = row["last_success_at"]
    return ScanState(
        source_url=row["source_url"],
        last_scanned_at=datetime.fromisoformat(last_scanned) if last_scanned else None,
        last_success_at=datetime.fromisoformat(last_success) if last_success else None,
        consecutive_failures=row["consecutive_failures"],
    )


def get_scan_state(conn: sqlite3.Connection, source_url: str) -> ScanState | None:
    """Return the `scan_state` row for `source_url`, or `None` if absent.

    `None` means the scheduler has never touched this URL — the caller uses
    it as the `first_run` signal for gate observability.
    """
    row = conn.execute(
        "SELECT source_url, last_scanned_at, last_success_at, consecutive_failures"
        " FROM scan_state WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    if row is None:
        return None
    return _scan_state_from_row(row)


def upsert_scan_state(conn: sqlite3.Connection, state: ScanState) -> None:
    """Insert or update the `scan_state` row keyed on `state.source_url`.

    Commits on success. `source_url` is the primary key, so `ON CONFLICT`
    updates every non-key column with the new value; a caller composing a
    tick's outcome sets all four fields on the incoming aggregate and this
    method stores the new snapshot verbatim.
    """
    conn.execute(
        "INSERT INTO scan_state"
        " (source_url, last_scanned_at, last_success_at, consecutive_failures)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(source_url) DO UPDATE SET"
        " last_scanned_at      = excluded.last_scanned_at,"
        " last_success_at      = excluded.last_success_at,"
        " consecutive_failures = excluded.consecutive_failures",
        (
            state.source_url,
            state.last_scanned_at.isoformat() if state.last_scanned_at is not None else None,
            state.last_success_at.isoformat() if state.last_success_at is not None else None,
            state.consecutive_failures,
        ),
    )
    conn.commit()


def bootstrap_system_user(conn: sqlite3.Connection) -> UserRecord:
    """Idempotently seed the `users` row every scheduler tick attributes work to.

    A second call returns the existing row (same `id`) — `get_or_create_user`
    is the primitive underneath and it is idempotent by `telegram_user_id`.
    Two concurrent `--tick` invocations both call this; the second sees the
    first's committed row and returns the same `id`. Matches the acceptance
    behaviour named in the plan's Risks + open questions.
    """
    return get_or_create_user(
        conn,
        telegram_user_id=SYSTEM_USER_TELEGRAM_ID,
        display_name=SYSTEM_USER_DISPLAY_NAME,
    )
