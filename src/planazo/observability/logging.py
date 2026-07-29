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
from collections.abc import Callable, Sequence
from datetime import datetime

from planazo.catalog.models import Event
from planazo.observability.models import AgentRunRecord, LLMDecision
from planazo.observability.repository import (
    record_agent_run,
    record_llm_decision,
    record_recommendations,
)

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


class LLMDecisionLogger:
    """Best-effort observer that persists 0..N `llm_decisions` rows per loop.

    Constructed once per composition root with a `conn_factory` that
    yields a fresh `sqlite3.Connection`. Callers build a list of
    validated `LLMDecision` rows at loop completion and hand them to
    `record_many()`; the writer opens one connection, inserts every
    row, closes the connection, and returns.

    Rule 4 hook: writer failures never propagate. Every exception from
    the `conn_factory` call, from any single `record_llm_decision`
    INSERT, or from the `conn.close()` in the `finally` is logged at
    WARNING and swallowed. Best-effort at the batch grain: if one row
    raises (an FK violation from a since-deleted run_id, a CHECK bypass
    from a hand-composed record) the surrounding INSERTs still commit
    up to the failing row, because `record_llm_decision` commits per
    call. A partial batch is more useful than a lost batch — an
    `agent_runs` row without every one of its decisions is still a
    queryable primary flow.

    The row-level try/except lives inside the loop so a mid-batch
    IntegrityError does not swallow the rest of the batch; the outer
    try/except catches setup / teardown failures instead.
    """

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def record_many(self, decisions: list[LLMDecision]) -> None:
        """Persist every `LLMDecision` in `decisions` best-effort.

        The outer try/except covers connection setup/teardown; the
        inner try/except per row means one bad row does not lose the
        rest of the batch. An empty list is a no-op — no connection is
        opened, matching the shape of the JSONL sidecar writer which
        also skips its I/O when the extractor produced no decisions.
        """
        if not decisions:
            return
        try:
            conn = self._conn_factory()
            try:
                for decision in decisions:
                    try:
                        record_llm_decision(conn, decision)
                    except Exception as exc:
                        logger.warning("llm_decision_logger write failed: %s", exc)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("llm_decision_logger write failed: %s", exc)


class RecommendationLogger:
    """Best-effort observer that persists 0..N `recommendations` rows per Recommender loop.

    Constructed once per composition root with a `conn_factory` that
    yields a fresh `sqlite3.Connection`. The Recommender hands the
    ordered candidate list at loop completion; the writer opens one
    connection, inserts every row atomically through `record_recommendations`,
    closes the connection, and returns.

    Rule 4 hook: writer failures never propagate. Every exception from
    the `conn_factory` call, from `record_recommendations` (an FK
    violation from an orphan `run_id` if `agent_runs` write failed, a
    disk-full error), or from `conn.close()` in the `finally` is logged
    at WARNING and swallowed. Batch-atomic: a mid-batch failure rolls
    the whole batch back — a partial candidate set with the top-ranked
    candidate missing would be worse than none for a `/find` history
    reader, because the ordering is only meaningful when complete.

    An empty candidate list is a no-op — no connection is opened,
    matching the shape of `LLMDecisionLogger.record_many`.
    """

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def record(
        self,
        run_id: str,
        ranked_events: Sequence[Event],
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist every candidate in `ranked_events` best-effort.

        An empty sequence is a no-op — no connection is opened. The
        primitive's atomic transaction means either every row lands or
        none of them do; the outer try/except catches every failure
        surface (connect, insert, close) and logs one WARNING.
        """
        if not ranked_events:
            return
        try:
            conn = self._conn_factory()
            try:
                record_recommendations(conn, run_id, ranked_events, now=now)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("recommendation_logger write failed: %s", exc)
