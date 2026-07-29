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
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.monitor.models import AgentName
from planazo.observability.models import (
    AgentRunRecord,
    DecisionKind,
    LLMDecision,
    RecommendationRecord,
)

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


def record_llm_decision(conn: sqlite3.Connection, decision: LLMDecision) -> int:
    """Insert one `llm_decisions` row for `decision` and return its row id.

    Commits on success. The four-way `decision_kind` consistency
    (`event_db_id` / `error_type` presence per branch) is guaranteed by
    `LLMDecision`'s `model_validator`; the `CHECK` on `decision_kind` in
    migration 004 is defense-in-depth for a raw-SQL diagnostic caller.
    Every raised `sqlite3.IntegrityError` — an FK violation from an
    orphan `run_id`, an FK violation from an `event_db_id` pointing at
    a since-deleted `events` row before `ON DELETE SET NULL` fires, a
    CHECK violation from a raw-SQL bypass — propagates. The best-effort
    suppression lives one layer up in the composition-root helpers,
    same discipline as `AgentRunLogger.record`.
    """
    cursor = conn.execute(
        "INSERT INTO llm_decisions"
        " (run_id, decision_kind, event_db_id, error_type, rationale, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            decision.run_id,
            decision.decision_kind,
            decision.event_db_id,
            decision.error_type,
            decision.rationale,
            decision.recorded_at.isoformat(),
        ),
    )
    conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        # See `record_agent_run` — SQLite always populates lastrowid on a
        # rowid table INSERT; a None here is a driver invariant break,
        # not a caller-facing branch.
        raise RuntimeError("record_llm_decision: sqlite3 lastrowid was None after INSERT")
    return row_id


def _decision_from_row(row: sqlite3.Row) -> LLMDecision | None:
    """Rehydrate one `LLMDecision` from a `sqlite3.Row`, or `None` if unrecoverable.

    Mirrors `_record_from_row`'s graceful discipline: a row whose stored
    shape no longer matches the current Pydantic aggregate (a legacy
    `decision_kind` value, a raw-SQL write that bypassed the sanitizer)
    is skipped with one WARNING log line naming the `run_id`, so the
    reader stays usable for the healthy rows around a bad one.
    """
    try:
        return LLMDecision.model_validate(dict(row))
    except ValidationError as exc:
        logger.warning(
            "query_llm_decisions: skipping row run_id=%s that failed re-validation: %s",
            row["run_id"],
            exc.errors(),
        )
        return None


def query_llm_decisions(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    decision_kind: DecisionKind | None = None,
    limit: int = 100,
) -> list[LLMDecision]:
    """Return persisted `llm_decisions` for the given filter, newest first.

    `run_id=None` returns rows across every run (corpus-analysis shape
    the M4 ranker will read). `decision_kind=None` returns rows across
    all four branches. Ordering by `recorded_at DESC` matches the
    inspection shape a future `/find` history projection or an operator
    trace reader will use; `limit` caps the result set.

    When `run_id` is filtered, `idx_llm_decisions_run` backs the query
    — an `EXPLAIN QUERY PLAN` test locks that choice, matching the
    pattern established for `idx_agent_runs_user_started` under T3.
    """
    if limit < 1:
        raise ValueError(f"query_llm_decisions limit must be >= 1, got {limit}")

    clauses: list[str] = []
    params: list[object] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if decision_kind is not None:
        clauses.append("decision_kind = ?")
        params.append(decision_kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(
        "SELECT run_id, decision_kind, event_db_id, error_type, rationale, recorded_at"
        f" FROM llm_decisions{where} ORDER BY recorded_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [decision for row in rows if (decision := _decision_from_row(row)) is not None]


def record_recommendations(
    conn: sqlite3.Connection,
    run_id: str,
    ranked_events: Sequence[Event],
    *,
    now: datetime | None = None,
) -> int:
    """Insert one `recommendations` row per candidate in `ranked_events`.

    `ranked_events` is the ordered sequence the Recommender surfaced —
    index 0 is the top-ranked candidate, `rank_position` is the index
    itself. Today (M3.7 T1) the Recommender does not invoke the
    deterministic ranker, so `score` and `reason` are persisted as
    `NULL` for every row; the follow-up ticket that wires `rank_events`
    at the composition root switches to `RankedEvent` and populates
    both columns.

    All rows share one `recorded_at` timestamp — the batch is a single
    atomic decision on the composition-root side. `now=None` reads
    `datetime.now(UTC)` at call time.

    Every row commits inside one transaction (single `conn.commit()`
    after the batch), so a mid-batch `IntegrityError` from an orphan
    `run_id` or a since-deleted `event_id` rolls the whole batch back —
    a `recommendations` row set is only useful atomically alongside its
    parent `agent_runs` row. Returns the count of rows inserted (0 for
    an empty sequence — no-op, no transaction opened). The best-effort
    suppression lives one layer up in `RecommendationLogger`, same
    discipline as `AgentRunLogger` / `LLMDecisionLogger`.
    """
    if not ranked_events:
        return 0
    stamped_at = (now or datetime.now(UTC)).isoformat()
    rows = [
        (run_id, event.id, position, None, None, stamped_at)
        for position, event in enumerate(ranked_events)
    ]
    conn.executemany(
        "INSERT INTO recommendations"
        " (run_id, event_id, rank_position, score, reason, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _recommendation_from_row(row: sqlite3.Row) -> RecommendationRecord | None:
    """Rehydrate one `RecommendationRecord` from a `sqlite3.Row`, or `None` if unrecoverable.

    Mirrors `_record_from_row`'s graceful discipline: a row whose stored
    shape no longer matches the current Pydantic aggregate (a `reason`
    with a raw control byte injected by a diagnostic tool, a negative
    `rank_position` from a raw-SQL bypass) is skipped with one WARNING
    naming the `run_id`, so the reader stays usable for the healthy
    rows around a bad one.
    """
    try:
        return RecommendationRecord.model_validate(dict(row))
    except ValidationError as exc:
        logger.warning(
            "query_recommendations: skipping row run_id=%s that failed re-validation: %s",
            row["run_id"],
            exc.errors(),
        )
        return None


def query_recommendations(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    user_id: int | None = None,
    limit: int = 100,
) -> list[RecommendationRecord]:
    """Return persisted `recommendations`, newest run first, rank ascending within a run.

    `run_id=None` returns rows across every run. `user_id=None` returns
    rows across every user. When `user_id` is supplied the query joins
    `agent_runs` to filter — the "history for one user" shape a `/find`
    reader will use. Rows are ordered by the parent `agent_runs.started_at`
    DESC (newest run first) then `rank_position` ASC (top candidate
    first within each run); `limit` caps the total row count.

    When `run_id` is filtered (no `user_id`), the `idx_recommendations_run_rank`
    composite index backs the WHERE-then-ORDER-BY plan — an
    `EXPLAIN QUERY PLAN` test locks that choice, matching the pattern
    established for `idx_agent_runs_user_started` / `idx_llm_decisions_run`.
    """
    if limit < 1:
        raise ValueError(f"query_recommendations limit must be >= 1, got {limit}")

    clauses: list[str] = []
    params: list[object] = []
    if user_id is not None:
        # `user_id` lives on `agent_runs`; join to filter, and let the
        # server-side sort span both tables to preserve "newest run first,
        # rank ascending within a run" across the join.
        join = " JOIN agent_runs ON agent_runs.run_id = recommendations.run_id"
        if run_id is not None:
            clauses.append("recommendations.run_id = ?")
            params.append(run_id)
        clauses.append("agent_runs.user_id = ?")
        params.append(user_id)
        where = f" WHERE {' AND '.join(clauses)}"
        order = " ORDER BY agent_runs.started_at DESC, recommendations.rank_position ASC"
        select_cols = (
            "recommendations.run_id, recommendations.event_id,"
            " recommendations.rank_position, recommendations.score,"
            " recommendations.reason, recommendations.recorded_at"
        )
        params.append(limit)
        rows = conn.execute(
            f"SELECT {select_cols} FROM recommendations{join}{where}{order} LIMIT ?",
            params,
        ).fetchall()
    else:
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # No `user_id` join → order by `recorded_at DESC` (newest run
        # first — every row in one batch shares its timestamp) then
        # `rank_position ASC`. When `run_id` is supplied the composite
        # `(run_id, rank_position)` index picks up the ORDER BY
        # `rank_position` clause directly.
        order = " ORDER BY recorded_at DESC, rank_position ASC"
        params.append(limit)
        rows = conn.execute(
            "SELECT run_id, event_id, rank_position, score, reason, recorded_at"
            f" FROM recommendations{where}{order} LIMIT ?",
            params,
        ).fetchall()
    return [rec for row in rows if (rec := _recommendation_from_row(row)) is not None]
