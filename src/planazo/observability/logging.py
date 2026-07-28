"""Best-effort SQLite writer that persists one `AgentRunRecord` per loop.

Mirrors the observer discipline of `planazo.monitor.logging.RunStepLogger`
and `planazo.extraction.audit.ExtractionRunLogger`, but with a different
grain: instead of one JSONL line per tool call, this writer emits one
DB row per completed loop. Composition roots (`agents.event_agent.run_once`,
`agents.extractor.extract_once`) instantiate it at the same seam they
already wire the JSONL loggers to, then call `record()` once at the end
of the loop with the built `AgentRunRecord`.

AGENTS.md rule 4 — audit failures never break the primary flow. Every
exception raised inside `record()` (a disk-full connect, a driver
version mismatch, an FK violation from a mis-seeded test) is logged at
WARNING level via the module logger and swallowed. The Recommender's
answer and the Extractor's `ExtractionResult` are the primary flow; a
missing `agent_runs` row degrades observability, not correctness.

The writer takes a `Callable[[], sqlite3.Connection]` rather than a
live connection so it can open + close a fresh short-lived connection
per `record()` call — matches the "connection is a per-call resource"
pattern already used by `event_agent.run_once._read_preferences` and
`extractor.extract_once` for the extraction-runs-index write.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from planazo.observability.models import AgentRunRecord
from planazo.observability.repository import record_agent_run

logger = logging.getLogger(__name__)


class AgentRunLogger:
    """Best-effort observer that persists one `agent_runs` row per loop.

    Constructed once per composition root with a `conn_factory` that
    yields a fresh `sqlite3.Connection`. Callers build a validated
    `AgentRunRecord` at loop completion and hand it to `record()`; the
    writer opens a connection through the factory, inserts the row,
    closes the connection, and returns. Any exception during that
    sequence is logged and swallowed — the caller's control flow is
    unchanged.
    """

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def record(self, record: AgentRunRecord) -> None:
        """Persist `record` best-effort, swallowing every exception.

        The `try` covers three failure surfaces: the `conn_factory` call
        itself (a disk-full mkdir, a stale schema that refuses to open),
        the `record_agent_run` INSERT (an FK/UNIQUE/CHECK violation, a
        driver error), and the `conn.close()` in the `finally` clause
        (rare, but if it raises the write already succeeded — we still
        swallow so a close-time error does not bubble to the caller).
        A single WARNING record on the module logger is the operator-
        facing signal; the JSONL sidecar remains the fallback audit
        surface until the observability DB is fixed.
        """
        try:
            conn = self._conn_factory()
            try:
                record_agent_run(conn, record)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("agent_run_logger write failed: %s", exc)
