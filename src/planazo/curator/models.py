"""Pydantic contracts for the curator's persistent state + audit log.

`CuratorState` mirrors the singleton row `curator_state` from migration
009. `CuratorRunRecord` is what the audit log at `var/curator_runs.jsonl`
carries — one JSON line per tick, matching the grain the scheduler uses
for `var/scheduler_runs.jsonl` (ADR 0011 §D8) but with counters shaped
for the curator's charter (archive / merge / re-categorize).

Both models are `extra="forbid"` so a caller composing a record without a
declared field trips `ValidationError` at construction; the DB layer
never sees an ill-shaped row.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_AUDIT_LOG_PATH: Final[Path] = Path("var/curator_runs.jsonl")
"""Where `append_run_record` writes `CuratorRunRecord` lines.

`var/` is `.gitignore`d — the file never enters version control. Tests
monkeypatch the destination to a `tmp_path` fixture; the CLI wires this
constant into the composition root at boot time. Matches the shape of
`scheduler.audit.DEFAULT_AUDIT_LOG_PATH`.
"""


class CuratorState(BaseModel):
    """One `curator_state` row — the singleton bookkeeping the curator upserts on every tick.

    `id` is fixed at 1 by the migration's `CHECK (id = 1)` — the singleton
    guard. The two `*_at` fields are `None` on a freshly-seeded row (the
    curator has not run yet); `consecutive_failures` starts at zero and
    never goes negative. The three `total_*` counters accumulate lifetime
    mutation counts and are never reset.
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(default=1, ge=1, le=1)
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    total_archived: int = Field(default=0, ge=0)
    total_merged: int = Field(default=0, ge=0)
    total_categories_fixed: int = Field(default=0, ge=0)


class CuratorRunRecord(BaseModel):
    """One JSONL line appended to `var/curator_runs.jsonl` per curator tick.

    Written under `curator.repository.append_run_record` at tick end; the
    reader is a human (`tail -f`) or a downstream aggregator, never the
    LLM loop. Grain is one record per tick — no per-decision fan-out into
    this file, because per-decision detail lives in `llm_decisions` with
    a matching `run_id`.

    The three mutation counters (`events_archived`, `events_merged`,
    `categories_updated`) sum the write-tool outcomes the LLM produced
    during this tick. `dry_run=True` still records the counters but
    they reflect what WOULD have been written — the write tools return
    the same shape whether or not they mutate the DB. `errors` is the
    typed audit trail for anything the LLM tried and the tool tier
    refused (invalid category, missing id, already archived).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    events_examined: int = Field(ge=0)
    events_archived: int = Field(ge=0)
    events_merged: int = Field(ge=0)
    categories_updated: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False
