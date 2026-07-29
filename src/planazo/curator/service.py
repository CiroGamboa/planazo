"""Curator composition root — reads state, runs the agent, upserts state.

`run_curator` is the function the CLI (T5) calls per tick. Mirrors
`scheduler.service.run_tick`'s shape: one connection per tick, read
prior state, invoke the primary work unit, write updated state, append
one audit-log line.

Rule 4 discipline: the audit-log append and the state upsert are both
best-effort. If either fails, the primary work unit (the LLM decisions
it already committed to the DB) stays valid. A WARNING is logged; the
tick still returns its `CuratorRunResult` so the operator sees what the
LLM decided even when observability broke.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from planazo.curator.agent import CuratorRunResult, run_curator_once
from planazo.curator.models import (
    DEFAULT_AUDIT_LOG_PATH,
    CuratorRunRecord,
    CuratorState,
)
from planazo.curator.repository import (
    append_run_record,
    get_state,
    upsert_state,
)
from planazo.storage import db

logger = logging.getLogger(__name__)


def run_curator(
    *,
    dry_run: bool = False,
    record_runs: bool = True,
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH,
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
    on_step: Any = None,
    on_complete: Any = None,
) -> CuratorRunResult:
    """Execute one curator tick end-to-end and return the typed result.

    Steps:
    1. Read the singleton `curator_state`.
    2. Invoke `run_curator_once(...)` — the primary work unit that talks
       to the LLM and mutates the catalog through curator tools.
    3. Upsert `curator_state` with new `last_run_at`, `last_success_at`,
       and lifetime counters. `consecutive_failures` resets on any
       `stopped="answered"` run; increments otherwise.
    4. Append one `CuratorRunRecord` line to `audit_log_path`.

    Steps 3 and 4 are best-effort (Rule 4). The primary DB mutations are
    already committed by the time we get here (they land inside the
    curator tools). An audit-log or state-upsert failure logs a WARNING
    but does not raise.

    `dry_run` flips the tools to no-op mode — the LLM sees the same
    interfaces and the audit trail still records what would have been
    written. `record_runs=False` skips the `agent_runs` /
    `llm_decisions` writers (test escape hatch).
    """
    resolved_conn_factory = conn_factory if conn_factory is not None else db.connect

    tick_result = run_curator_once(
        dry_run=dry_run,
        record_runs=record_runs,
        on_step=on_step,
        on_complete=on_complete,
    )

    _upsert_state_best_effort(
        tick_result=tick_result,
        conn_factory=resolved_conn_factory,
        dry_run=dry_run,
    )
    _append_run_record_best_effort(
        tick_result=tick_result,
        audit_log_path=audit_log_path,
    )
    return tick_result


def _upsert_state_best_effort(
    *,
    tick_result: CuratorRunResult,
    conn_factory: Callable[[], sqlite3.Connection],
    dry_run: bool,
) -> None:
    """Update the singleton `curator_state` with the tick's outcome.

    Reads the current state, composes the new snapshot, writes it.
    Every step is inside a try/except so a driver error, an FK violation
    (impossible for this table but defensive), or a filesystem hiccup
    logs a WARNING and returns rather than propagating.

    `dry_run=True` still updates `last_run_at` and the lifetime counters
    (since we track what the LLM decided) — an operator can distinguish
    dry-run ticks from wet ticks via the `dry_run` flag in the audit log.
    """
    try:
        conn = conn_factory()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("curator_state: open failed: %s", type(exc).__name__)
        return
    try:
        try:
            current = get_state(conn)
        except sqlite3.Error as exc:
            logger.warning("curator_state: read failed: %s", type(exc).__name__)
            return
        succeeded = tick_result.stopped == "answered"
        updated = CuratorState(
            last_run_at=tick_result.ended_at,
            last_success_at=(tick_result.ended_at if succeeded else current.last_success_at),
            consecutive_failures=0 if succeeded else current.consecutive_failures + 1,
            total_archived=(
                current.total_archived + tick_result.events_archived - tick_result.events_merged
            ),
            total_merged=current.total_merged + tick_result.events_merged,
            total_categories_fixed=(
                current.total_categories_fixed + tick_result.categories_updated
            ),
        )
        try:
            upsert_state(conn, updated)
        except sqlite3.Error as exc:
            logger.warning("curator_state: upsert failed: %s", type(exc).__name__)
    finally:
        conn.close()


def _append_run_record_best_effort(
    *,
    tick_result: CuratorRunResult,
    audit_log_path: Path,
) -> None:
    """Append one `CuratorRunRecord` line to `audit_log_path`.

    Wrapper around `curator.repository.append_run_record` that swallows
    every raise (Rule 4). The tick's primary flow — the DB decisions the
    curator tools already committed — is unaffected.
    """
    try:
        record = CuratorRunRecord(
            run_id=tick_result.run_id,
            started_at=tick_result.started_at,
            ended_at=tick_result.ended_at,
            events_examined=tick_result.events_examined,
            events_archived=tick_result.events_archived,
            events_merged=tick_result.events_merged,
            categories_updated=tick_result.categories_updated,
            errors=tick_result.errors,
            dry_run=tick_result.dry_run,
        )
        append_run_record(record, audit_log_path)
    except (OSError, ValueError) as exc:
        logger.warning("curator_runs.jsonl: append failed: %s", type(exc).__name__)


def _now() -> datetime:
    """Current time in UTC. Injection seam for tests that want a fixed clock."""
    return datetime.now(UTC)
