"""Observability repository — narrow, connection-parameterized SQL for `agent_runs`.

Same two-tier pattern as `catalog/repository.py` and
`scheduler/repository.py` (per ADR 0003): functions take an explicit
`sqlite3.Connection` and never open one themselves. Composing across
several primitives (a test against `":memory:"`, a future `/find`
history reader) runs against a single connection.

The write primitive commits on success. `sqlite3.IntegrityError`
propagates: the CHECK on `agent_kind`, the `UNIQUE` on `run_id`, and the
FK on `user_id` are boundary locks, and a violation is a caller bug that
the composition root should see rather than swallow. The best-effort
suppression lives one layer up in `observability.logging.AgentRunLogger`
so the same primitive is usable from both the audit writer (best-effort,
never crashes the primary flow) and any future reader/test that wants
the exception at construction time.

The read primitive `query_agent_runs` is graceful under row-level
corruption: a row that fails Pydantic re-validation (someone wrote raw
SQL and bypassed the sanitizer, an older migration left a value outside
the current Literal, etc.) is skipped with one WARNING per bad row so
the operator can still inspect the healthy rows around it. Loud-failure
would let one corrupt row block the whole history view — an outcome
worse than dropping that row's visibility until the write path is
fixed.
"""

from __future__ import annotations

import logging
import sqlite3

from pydantic import ValidationError

from planazo.monitor.models import AgentName
from planazo.observability.models import AgentRunRecord

logger = logging.getLogger(__name__)


def record_agent_run(conn: sqlite3.Connection, record: AgentRunRecord) -> int:
    """Insert one `agent_runs` row for `record` and return its row id.

    Commits on success. The sanitized-text invariant is guaranteed by
    `AgentRunRecord`'s after-validator; the CHECK on `agent_kind` is a
    defense-in-depth boundary lock at the DB layer. A `run_id` collision
    raises `sqlite3.IntegrityError` — `run_id` is unique per run and a
    collision is a caller bug (a UUID-generation mishap, a test that
    reuses an id across fixtures).
    """
    cursor = conn.execute(
        "INSERT INTO agent_runs"
        " (run_id, agent_kind, user_id, user_query, final_answer, stopped,"
        "  steps_count, started_at, ended_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.run_id,
            record.agent_kind,
            record.user_id,
            record.user_query,
            record.final_answer,
            record.stopped,
            record.steps_count,
            record.started_at.isoformat(),
            record.ended_at.isoformat(),
        ),
    )
    conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        # SQLite's INSERT always populates lastrowid unless the table has
        # WITHOUT ROWID (which `agent_runs` does not). A None here is a
        # driver invariant break, not a caller-facing branch.
        raise RuntimeError("record_agent_run: sqlite3 lastrowid was None after INSERT")
    return row_id


def _record_from_row(row: sqlite3.Row) -> AgentRunRecord | None:
    """Rehydrate one `AgentRunRecord` from a `sqlite3.Row`, or `None` if unrecoverable.

    Passes through the Pydantic aggregate so the sanitization invariant
    round-trips. If validation fails — a corrupt row from a diagnostic
    tool that bypassed the sanitizer, a value written before a Literal
    was tightened — the row is skipped with one WARNING that names the
    row's `run_id` (safe to log; run_id is a UUID, never caption
    content) and its `ValidationError.errors()` shape. The caller
    filters `None` results out of the list. This keeps the reader
    usable in the face of a single bad row instead of failing the
    whole query.
    """
    try:
        return AgentRunRecord.model_validate(dict(row))
    except ValidationError as exc:
        # `_record_from_row` is private and only called from `query_agent_runs`,
        # whose SELECT always includes `run_id` — no fallback needed.
        logger.warning(
            "query_agent_runs: skipping row run_id=%s that failed re-validation: %s",
            row["run_id"],
            exc.errors(),
        )
        return None


def query_agent_runs(
    conn: sqlite3.Connection,
    *,
    user_id: int | None = None,
    agent_kind: AgentName | None = None,
    limit: int = 100,
) -> list[AgentRunRecord]:
    """Return persisted `agent_runs` for the given filter, newest first.

    `user_id=None` returns rows across every user (operator queries).
    `agent_kind=None` returns rows across both kinds. The query orders
    by `started_at DESC` so history readers see the most recent runs
    first; `limit` caps the result set. When `user_id` is filtered the
    `idx_agent_runs_user_started` composite index backs the query — a
    read of the EXPLAIN QUERY PLAN under test locks that choice.
    """
    if limit < 1:
        raise ValueError(f"query_agent_runs limit must be >= 1, got {limit}")

    clauses: list[str] = []
    params: list[object] = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if agent_kind is not None:
        clauses.append("agent_kind = ?")
        params.append(agent_kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(
        "SELECT run_id, agent_kind, user_id, user_query, final_answer, stopped,"
        " steps_count, started_at, ended_at"
        f" FROM agent_runs{where} ORDER BY started_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [record for row in rows if (record := _record_from_row(row)) is not None]
