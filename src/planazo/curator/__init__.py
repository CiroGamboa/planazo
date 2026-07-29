"""Catalog curator bounded context — see docs/adr/0020-catalog-curator-agent.md.

The curator is an admin-scoped LLM agent that runs on a daily cron and
prunes the `events` catalog: soft-deletes stale events, merges duplicates,
corrects mis-classifications. It has capabilities the Recommender and
Extractor deliberately don't — write access to arbitrary event rows via
the soft-delete + category-update primitives added by T1.

Bounded-context surface:

- `curator.models` — `CuratorState` (singleton bookkeeping row) and
  `CuratorRunRecord` (one JSONL line per tick).
- `curator.repository` — CRUD for `curator_state` and the `append_run_record`
  helper for `var/curator_runs.jsonl`.
- `curator.service` — `run_curator()` composition root (T4).
- `curator.agent` — the LLM loop (T4).
- `curator.tools` — the six LLM tools (T3).
- `curator.cli` — `planazo-curator` entrypoint (T5).

Only `models`, `repository`, and the audit-log helper land in T2; the
agent + tools + CLI come in the follow-on tickets so each PR stays
reviewable in isolation.
"""

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

__all__ = [
    "DEFAULT_AUDIT_LOG_PATH",
    "CuratorRunRecord",
    "CuratorState",
    "append_run_record",
    "get_state",
    "upsert_state",
]
