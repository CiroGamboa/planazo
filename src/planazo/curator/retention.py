"""Retention sweep for curator-archived events (ADR 0020 follow-up).

`run_retention` is a small standalone composition root — no LLM, no
tools. It complements the curator's LLM loop by physically deleting
events that have been soft-deleted (`archived_at IS NOT NULL`) for
longer than a configurable number of days.

Rationale for a physical delete on already-archived rows:

- The soft-delete lifecycle (T1) gives operators the reversibility
  guarantee ADR 0020 §D1 rests on. Once N days have passed without a
  reversal, the operator has effectively confirmed the archive; the row
  becomes eligible for physical delete.
- FK behavior on `llm_decisions.event_db_id` and
  `recommendations.event_id` is `ON DELETE SET NULL` — the audit trail
  loses its pointer but the rationale + reason text stay. An operator
  can still read "curator archived event 42 with reason X" after the
  hard delete.

`dry_run=True` produces the same `RetentionResult` shape but with
`deleted=0`; the `preview` list carries what WOULD have been deleted.
Matches the `--dry-run` semantics the LLM loop uses.

Rule 4: `_append_run_record_best_effort` swallows filesystem errors on
the audit log. The DELETE has already committed by the time we get
there.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

from planazo.catalog.models import Event
from planazo.catalog.repository import (
    list_purgeable_archived_events,
    purge_archived_events_older_than,
)
from planazo.curator.models import DEFAULT_AUDIT_LOG_PATH, CuratorRunRecord
from planazo.curator.notifier import notify_admins_of_retention
from planazo.curator.repository import append_run_record
from planazo.storage import db

logger = logging.getLogger(__name__)

MIN_RETENTION_DAYS: Final[int] = 1
"""Below this the flag is refused — a same-day sweep is almost always a bug."""

MAX_RETENTION_DAYS: Final[int] = 3650
"""~10 years. The upper bound is a sanity check; nothing in the schema cares."""

_PREVIEW_LIMIT: Final[int] = 500
"""How many rows `list_purgeable_archived_events` returns in dry-run mode."""


@dataclass(frozen=True)
class RetentionResult:
    """The typed return of one `run_retention` invocation.

    Not persisted to any DB row by itself — the tick's audit line lands
    in `var/curator_runs.jsonl` as a `CuratorRunRecord` with `dry_run`
    matching the input flag. The `preview` list is memory-only.
    """

    run_id: str
    retention_days: int
    cutoff: datetime
    deleted: int
    preview: list[Event]
    dry_run: bool
    started_at: datetime
    ended_at: datetime


def run_retention(
    *,
    retention_days: int,
    dry_run: bool = False,
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH,
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
    now: Callable[[], datetime] | None = None,
) -> RetentionResult:
    """Sweep archived events older than `retention_days`.

    Steps:
    1. Compute `cutoff = now() - timedelta(days=retention_days)`.
    2. Open a connection.
    3. Read up to `_PREVIEW_LIMIT` purgeable rows (for the `preview`
       list on the return — matters for `--verbose` operator output).
    4. If `dry_run=False`, execute the DELETE. If `dry_run=True`, skip
       the DELETE and return `deleted=0`.
    5. Close the connection.
    6. Append one `CuratorRunRecord` line to `audit_log_path` with the
       retention tick's counters (best-effort).

    Bounds `retention_days` to `[MIN_RETENTION_DAYS, MAX_RETENTION_DAYS]`;
    values outside raise `ValueError`. Callers (CLI, tests) validate
    upfront — this is defense-in-depth.
    """
    if not MIN_RETENTION_DAYS <= retention_days <= MAX_RETENTION_DAYS:
        raise ValueError(
            f"retention_days must be in [{MIN_RETENTION_DAYS}, "
            f"{MAX_RETENTION_DAYS}], got {retention_days}"
        )
    resolved_now = now if now is not None else _now
    resolved_conn_factory = conn_factory if conn_factory is not None else db.connect
    started_at = resolved_now()
    cutoff = started_at - timedelta(days=retention_days)

    conn = resolved_conn_factory()
    try:
        preview = list_purgeable_archived_events(conn, cutoff=cutoff, limit=_PREVIEW_LIMIT)
        deleted = 0
        if not dry_run:
            deleted = purge_archived_events_older_than(conn, cutoff=cutoff)
    finally:
        conn.close()

    ended_at = resolved_now()
    result = RetentionResult(
        run_id=str(uuid4()),
        retention_days=retention_days,
        cutoff=cutoff,
        deleted=deleted,
        preview=preview,
        dry_run=dry_run,
        started_at=started_at,
        ended_at=ended_at,
    )
    _append_run_record_best_effort(result=result, audit_log_path=audit_log_path)
    # Rule 4 — the notifier itself catches every failure surface; the
    # belt-and-braces wrapper catches a hypothetical contract change.
    try:
        notify_admins_of_retention(result)
    except Exception as exc:
        logger.warning("curator.notifier: notify_admins_of_retention raised %s", type(exc).__name__)
    return result


def _append_run_record_best_effort(
    *,
    result: RetentionResult,
    audit_log_path: Path,
) -> None:
    """Append one `CuratorRunRecord` for the retention tick.

    Reuses the curator's audit-log grain so an operator's `tail -f
    var/curator_runs.jsonl` sees retention ticks alongside LLM ticks.
    `events_examined` = `len(result.preview)` (what the CLI showed the
    operator); `events_archived` = `deleted` (what got hard-deleted).
    `events_merged` + `categories_updated` are zero — retention doesn't
    merge or re-categorize.

    Rule 4: swallow every filesystem raise.
    """
    try:
        record = CuratorRunRecord(
            run_id=result.run_id,
            started_at=result.started_at,
            ended_at=result.ended_at,
            events_examined=len(result.preview),
            events_archived=result.deleted,
            events_merged=0,
            categories_updated=0,
            errors=[],
            dry_run=result.dry_run,
        )
        append_run_record(record, audit_log_path)
    except (OSError, ValueError) as exc:
        logger.warning("curator_runs.jsonl: retention append failed: %s", type(exc).__name__)


def _now() -> datetime:
    """Current time in UTC. Injection seam for tests."""
    return datetime.now(UTC)
