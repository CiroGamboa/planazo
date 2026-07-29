"""Curator repository — CRUD for `curator_state` + `var/curator_runs.jsonl` append helper.

Two-tier discipline (ADR 0003): the primitives here take an explicit
`sqlite3.Connection` (or `Path`) and never open one themselves. Callers are
`curator.service` (composition root, T4) and tests. The Recommender and
Extractor never reach in here — the curator's state is admin-owned.

Mirrors `scheduler.repository`'s shape (`get_scan_state` / `upsert_scan_state`)
and `scheduler.audit.append_run_record`'s JSONL contract, adapted to the
curator's singleton state and its own audit log path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from planazo.curator.models import CuratorRunRecord, CuratorState


def _curator_state_from_row(row: sqlite3.Row) -> CuratorState:
    last_run = row["last_run_at"]
    last_success = row["last_success_at"]
    return CuratorState(
        id=row["id"],
        last_run_at=datetime.fromisoformat(last_run) if last_run else None,
        last_success_at=datetime.fromisoformat(last_success) if last_success else None,
        consecutive_failures=row["consecutive_failures"],
        total_archived=row["total_archived"],
        total_merged=row["total_merged"],
        total_categories_fixed=row["total_categories_fixed"],
    )


def get_state(conn: sqlite3.Connection) -> CuratorState:
    """Return the singleton `curator_state` row.

    Never raises `None` — migration 009 seeds the row via `INSERT OR IGNORE`
    so a fresh DB reads back a defaults-only state. If the row is somehow
    missing (a corrupted DB), `sqlite3.DatabaseError` propagates rather
    than a `None` sentinel that would silently disable curator bookkeeping.
    """
    row = conn.execute(
        "SELECT id, last_run_at, last_success_at, consecutive_failures,"
        " total_archived, total_merged, total_categories_fixed"
        " FROM curator_state WHERE id = 1"
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError(
            "curator_state row is missing — migration 009 should have seeded it"
        )
    return _curator_state_from_row(row)


def upsert_state(conn: sqlite3.Connection, state: CuratorState) -> None:
    """Overwrite the singleton `curator_state` row with `state`.

    Uses `UPDATE` — the row is guaranteed to exist by migration 009, and
    `state.id` is Pydantic-locked to 1. Commits on success. Callers pass
    the fully-composed post-tick state; this method does not merge with
    prior values (that composition is `curator.service`'s responsibility).
    """
    conn.execute(
        "UPDATE curator_state SET"
        " last_run_at = ?,"
        " last_success_at = ?,"
        " consecutive_failures = ?,"
        " total_archived = ?,"
        " total_merged = ?,"
        " total_categories_fixed = ?"
        " WHERE id = 1",
        (
            state.last_run_at.isoformat() if state.last_run_at is not None else None,
            state.last_success_at.isoformat() if state.last_success_at is not None else None,
            state.consecutive_failures,
            state.total_archived,
            state.total_merged,
            state.total_categories_fixed,
        ),
    )
    conn.commit()


def append_run_record(record: CuratorRunRecord, path: Path) -> None:
    """Append one JSON-serialised `record` line to `path`.

    Creates the parent directory if it does not exist. Uses compact JSON
    separators (`(",", ":")`) to match `scheduler.audit.append_run_record`
    and `monitor/logging.py` so `tail -f var/curator_runs.jsonl` shows one
    record per terminal row.

    Rule 4 discipline: the caller wraps this in a best-effort try/except at
    the composition root. This primitive is loud — a filesystem failure
    raises rather than swallowing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record.model_dump(mode="json"), separators=(",", ":")))
        log_file.write("\n")
