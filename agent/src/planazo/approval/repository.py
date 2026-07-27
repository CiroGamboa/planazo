"""Approval-gate audit trail repository — narrow, connection-parameterized SQL.

Matches the two-tier pattern documented in ADR 0003: both functions take an
explicit `sqlite3.Connection` and never open one themselves. Composing across
several primitives (a push-context assembly, a test against `":memory:"`)
runs against a single connection. `sqlite3.IntegrityError` is allowed to
propagate — these primitives are not LLM-reachable; only our own composition
code and tests call them, so a bad `user_id` is a loud-failure bug, not a
named error branch.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from planazo.approval.models import ApprovalDecision


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    """The row id of the INSERT `cursor` just executed."""
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("expected an INSERT cursor with a lastrowid, got None")
    return row_id


def record_approval(conn: sqlite3.Connection, approval: ApprovalDecision) -> int:
    """Append one approval-gate decision to the audit trail; return its row id."""
    decided_at = approval.decided_at or datetime.now(UTC)
    cursor = conn.execute(
        "INSERT INTO approvals (user_id, artifact_kind, artifact_id, decision, decided_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            approval.user_id,
            approval.artifact_kind,
            approval.artifact_id,
            approval.decision,
            decided_at.isoformat(),
        ),
    )
    conn.commit()
    return _last_row_id(cursor)


def list_approvals(conn: sqlite3.Connection, user_id: int) -> list[ApprovalDecision]:
    """Return every recorded decision for `user_id`, earliest first."""
    rows = conn.execute(
        "SELECT * FROM approvals WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [
        ApprovalDecision(
            id=row["id"],
            user_id=row["user_id"],
            artifact_kind=row["artifact_kind"],
            artifact_id=row["artifact_id"],
            decision=row["decision"],
            decided_at=datetime.fromisoformat(row["decided_at"]),
        )
        for row in rows
    ]
