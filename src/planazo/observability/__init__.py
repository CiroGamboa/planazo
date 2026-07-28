"""Observability bounded context — persisted `agent_runs` rows.

Peer of `scheduler/` on the write side: the Recommender and the Extractor
composition roots build a validated `AgentRunRecord` at the end of each
loop and hand it to `AgentRunLogger.record` for a best-effort SQLite
insert. Everything the operator wants to query relationally against the
`events` and `users` tables lives here; the full trace grain still rides
in the JSONL sidecars (`data/runs/{run_id}.jsonl`,
`var/extraction_runs.jsonl`) — SQLite persistence sits alongside them
rather than replacing them (ADR 0015 / issue #89 §Out of scope).

The context is write-side only. Composition roots wire the writer from
outside; nothing under `agents/`, `catalog/`, or `extraction/` is
imported from here (ADR 0008 bounded-context boundary), so a rewrite of
the write path can move independently of the read path — which lands in
a later ticket for the `/find` history view (#23).
"""

from planazo.observability.logging import AgentRunLogger
from planazo.observability.models import (
    FINAL_ANSWER_CAP,
    USER_QUERY_CAP,
    AgentRunRecord,
    format_stored_text,
)
from planazo.observability.repository import query_agent_runs, record_agent_run

__all__ = [
    "FINAL_ANSWER_CAP",
    "USER_QUERY_CAP",
    "AgentRunLogger",
    "AgentRunRecord",
    "format_stored_text",
    "query_agent_runs",
    "record_agent_run",
]
