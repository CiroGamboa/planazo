"""Catalog curator agent — composes tools + system prompt + `run_loop`.

`run_curator_once` is the composition root the CLI calls per tick. It binds
the six curator tools (T3) to `agentlib.run_loop` at STRONG tier, wires
`AgentRunLogger` + `LLMDecisionLogger` with `agent_kind="curator"`, and
returns a typed `CuratorRunResult` the caller uses to update
`curator_state` and append the audit-log line.

Trust boundary (Rule 8): the curator reads untrusted content (`Event.title`,
`description`, `venue_name` — Extractor output from Instagram captions) and
calls its own tools. Same posture as the Extractor. No output crosses back
to a user-facing surface — the run's decisions land in `agent_runs` /
`llm_decisions` / `var/curator_runs.jsonl`, never in a Telegram reply.

Rationale strings written to `llm_decisions.rationale` are DB-inside per
Rule 2 (matches ADR 0015 discipline for `llm_decisions`).

`record_runs=False` is the test escape hatch — no `agent_runs` /
`llm_decisions` writes fire. `dry_run=True` flips the write tools to
no-op returns; reads still happen and the audit trail still records
what would have been written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast
from uuid import uuid4

from agentlib.core import STRONG
from planazo.agents.loop import LoopResult, StepRecord, run_loop
from planazo.curator.tools import build_curator_tools
from planazo.observability.logging import AgentRunLogger, LLMDecisionLogger
from planazo.observability.models import (
    FINAL_ANSWER_CAP,
    RATIONALE_CAP,
    USER_QUERY_CAP,
    AgentRunRecord,
    LLMDecision,
    format_stored_text,
)
from planazo.storage import db
from tools.schema import schema_for

MAX_STEPS: Final[int] = 12
"""How many LLM turns the curator loop may run before `max_steps` fires.

12 covers: three read-tool calls (list_stale, list_duplicates,
list_low_confidence) + up to ~9 write-tool calls per tick. A tick that
needs more writes gets `truncated` / `max_steps` and comes back tomorrow
— the audit trail records the truncation and the operator sees the
counter climb.
"""

MAX_OUTPUT_TOKENS: Final[int] = 2000
"""Max tokens the LLM may produce per turn.

Higher than the Recommender (which writes user-facing prose) because the
curator sometimes explains its decision at length in the `reason`
argument passed to write tools. Still small — 2000 tokens is ~1500 words
of English, well above what a curator ever needs.
"""

USER_MESSAGE: Final[str] = (
    "Do one curator tick. Review the catalog and archive stale events, "
    "merge duplicates, and correct mis-classified categories. Stop when "
    "the read tools return no actionable rows."
)
"""Fixed user prompt — the caption of the loop.

Analogous to `extractor.USER_MESSAGE`. The LLM's actual work is triggered
by the system prompt (the charter). This message is what a human operator
would type if they invoked the curator by hand.
"""

_SYSTEM_PROMPT: Final[str] = f"""You are Planazo's catalog curator. Clean \
the shared events database by archiving stale events, merging obvious \
duplicates, and correcting mis-classified categories.

Read tools (call these first):
- list_stale_events(limit): events past their end_utc.
- list_duplicate_candidates(limit): groups by normalized title + date + venue (exact match).
- list_fuzzy_duplicate_candidates(similarity_threshold=0.6, limit): groups by \
date + venue where titles are similar-but-not-identical (Jaccard token overlap).
- list_low_confidence_events(threshold=0.4, limit): extractor-uncertain rows.

Write tools (require a `reason` <= 500 chars):
- archive_event(event_id, reason): soft-delete one event.
- merge_events(keep_event_id, archive_event_ids, reason): keep one row, \
archive the rest of the group.
- update_event_category(event_id, new_category, reason): correct a category.

EventCategory values MUST be one of: tech, cultural, music, networking, \
sports, other. Anything else is rejected.

Guidelines:
1. Start with list_stale_events. Archive any event whose end_utc is a \
full day past.
2. Then list_duplicate_candidates. Within a group, pick the row with \
highest confidence as keep_event_id; merge the rest.
3. Then list_low_confidence_events. If the title maps to a clear \
category, call update_event_category. If the row is spam or non-event \
content, archive it.
4. Every write requires a `reason` explaining the decision.
5. Stop when reads return no actionable rows or you have made \
approximately 10 writes.

You have {MAX_STEPS} steps total. Read calls count as steps."""


@dataclass(frozen=True)
class CuratorRunResult:
    """The typed return of one `run_curator_once` invocation.

    The caller (`curator.service.run_curator`) uses these counters to
    update `curator_state` and to compose the `CuratorRunRecord` for the
    JSONL audit log. Not persisted to any DB row by itself — every
    persistent artifact goes through `agent_runs`, `llm_decisions`, or
    the audit log.
    """

    run_id: str
    stopped: Literal["answered", "truncated", "max_steps"]
    steps: int
    events_examined: int
    events_archived: int
    events_merged: int
    categories_updated: int
    errors: list[str]
    dry_run: bool
    started_at: datetime
    ended_at: datetime


def run_curator_once(
    *,
    dry_run: bool = False,
    record_runs: bool = True,
    on_step: Any = None,
    on_complete: Any = None,
) -> CuratorRunResult:
    """Run one curator tick end-to-end and return the typed result.

    Composition:
    - `build_curator_tools(dry_run=dry_run)` returns the six-tool registry
      with dry_run closed over the writes (T3).
    - `run_loop` (STRONG tier, max_steps=12) drives the LLM.
    - `AgentRunLogger` writes one `agent_runs` row per tick;
      `LLMDecisionLogger.record_many` writes one `llm_decisions` row per
      write-tool call.
    - `record_runs=False` skips both writers (test escape hatch).

    `dry_run` DOES NOT affect observability: the audit trail records what
    the LLM decided, whether or not the DB was mutated. This is
    intentional — an operator running `--dry-run` still gets a full
    `agent_runs` row + `llm_decisions` rationale trail to review.

    `on_step` / `on_complete` are optional observer seams matching
    `extract_once`'s shape — the T5 CLI uses them to plumb the narrative
    logger into `--verbose`.
    """
    run_id = str(uuid4())
    tools = build_curator_tools(dry_run=dry_run)
    tool_schemas: list[dict[str, Any]] = [
        schema_for(tools["list_stale_events"]),
        schema_for(tools["list_duplicate_candidates"]),
        schema_for(tools["list_fuzzy_duplicate_candidates"]),
        schema_for(tools["list_low_confidence_events"]),
        schema_for(tools["archive_event"]),
        schema_for(tools["merge_events"]),
        schema_for(tools["update_event_category"]),
    ]
    registry: dict[str, Any] = dict(tools)

    trace: list[StepRecord] = []

    def observe(record: StepRecord) -> None:
        trace.append(record)
        if on_step is not None:
            on_step(record)

    started_at = datetime.now(UTC)
    loop_result = run_loop(
        user_message=USER_MESSAGE,
        tools=tool_schemas,
        registry=registry,
        model=STRONG,
        max_steps=MAX_STEPS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        on_step=observe,
        system=_SYSTEM_PROMPT,
    )
    ended_at = datetime.now(UTC)

    if on_complete is not None:
        on_complete(loop_result)

    if record_runs:
        _record_agent_run_best_effort(
            run_id=run_id,
            result=loop_result,
            started_at=started_at,
            ended_at=ended_at,
        )
        _record_llm_decisions_best_effort(
            run_id=run_id,
            trace=trace,
            result=loop_result,
            recorded_at=ended_at,
        )

    counters = _count_write_outcomes(trace)
    errors = _collect_write_errors(trace)
    stopped_literal: Literal["answered", "truncated", "max_steps"]
    if loop_result.stopped in {"answered", "truncated", "max_steps"}:
        stopped_literal = loop_result.stopped
    else:
        stopped_literal = "answered"
    return CuratorRunResult(
        run_id=run_id,
        stopped=stopped_literal,
        steps=loop_result.steps,
        events_examined=_count_events_examined(trace),
        events_archived=counters["archive"] + counters["merge"],
        events_merged=counters["merge"],
        categories_updated=counters["update_category"],
        errors=errors,
        dry_run=dry_run,
        started_at=started_at,
        ended_at=ended_at,
    )


def _record_agent_run_best_effort(
    *,
    run_id: str,
    result: LoopResult,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    """Write one `agent_runs` row for this curator tick.

    Curator runs are system-owned — no `user_id` attribution, matching
    the scheduler's system-user pattern. `AgentRunLogger` catches every
    raise (Rule 4); the tick's primary flow is the DB mutations, and
    an audit-log failure must not block them.
    """
    assert result.stopped not in {"preference_read_error", "missing_search_origin"}, (
        "curator loop never surfaces Recommender-only pre-run failures"
    )
    stopped_literal = cast("Literal['answered', 'truncated', 'max_steps']", result.stopped)
    record = AgentRunRecord(
        run_id=run_id,
        agent_kind="curator",
        user_id=None,
        user_query=format_stored_text(USER_MESSAGE, USER_QUERY_CAP),
        final_answer=(
            format_stored_text(result.answer, FINAL_ANSWER_CAP)
            if result.answer is not None
            else None
        ),
        stopped=stopped_literal,
        steps_count=result.steps,
        started_at=started_at,
        ended_at=ended_at,
    )
    AgentRunLogger(conn_factory=db.connect).record(record)


def _record_llm_decisions_best_effort(
    *,
    run_id: str,
    trace: list[StepRecord],
    result: LoopResult,
    recorded_at: datetime,
) -> None:
    """Emit one `LLMDecision` per successful write-tool call + loop-terminal error.

    Only successful writes produce a decision row — a failed `archive_event`
    (`not_found`, `already_archived`) is visible in `var/curator_runs.jsonl`
    via `errors[]` and stays out of the `llm_decisions` corpus so the monitor's
    reads on "what did the curator actually mutate?" stay clean.

    `merge_events` produces N rows — one per archived id — so the corpus
    accurately reflects "N events were archived under this merge decision".
    Rationale is the LLM's `reason` argument, sanitized via
    `format_stored_text`.

    Best-effort: `LLMDecisionLogger.record_many` swallows every raise with
    a WARNING log line (Rule 4).
    """
    decisions: list[LLMDecision] = []
    for record in trace:
        if not isinstance(record.result, dict):
            continue
        if record.result.get("status") != "ok":
            # dry_run returns "dry_run" — a real decision on the LLM's
            # side, but no mutation happened; skip llm_decisions and let
            # the audit log capture what would have been written.
            continue
        reason = record.arguments.get("reason", "") if isinstance(record.arguments, dict) else ""
        rationale = format_stored_text(str(reason), RATIONALE_CAP) or "no rationale supplied"
        if record.tool == "archive_event":
            event_db_id = record.result.get("archived_event_id")
            if isinstance(event_db_id, int):
                decisions.append(
                    LLMDecision(
                        run_id=run_id,
                        decision_kind="archive",
                        event_db_id=event_db_id,
                        error_type=None,
                        rationale=rationale,
                        recorded_at=recorded_at,
                    )
                )
        elif record.tool == "merge_events":
            archived_ids = record.result.get("archived_event_ids", [])
            if isinstance(archived_ids, list):
                for archived_id in archived_ids:
                    if isinstance(archived_id, int):
                        decisions.append(
                            LLMDecision(
                                run_id=run_id,
                                decision_kind="merge",
                                event_db_id=archived_id,
                                error_type=None,
                                rationale=rationale,
                                recorded_at=recorded_at,
                            )
                        )
        elif record.tool == "update_event_category":
            event_db_id = record.result.get("event_id")
            if isinstance(event_db_id, int):
                decisions.append(
                    LLMDecision(
                        run_id=run_id,
                        decision_kind="update_category",
                        event_db_id=event_db_id,
                        error_type=None,
                        rationale=rationale,
                        recorded_at=recorded_at,
                    )
                )

    if result.stopped in ("truncated", "max_steps"):
        stopped_str = result.stopped
        decisions.append(
            LLMDecision(
                run_id=run_id,
                decision_kind="error",
                event_db_id=None,
                error_type=("loop_truncated" if stopped_str == "truncated" else "max_steps"),
                rationale=(
                    "loop truncated mid-turn" if stopped_str == "truncated" else "max_steps reached"
                ),
                recorded_at=recorded_at,
            )
        )

    LLMDecisionLogger(conn_factory=db.connect).record_many(decisions)


_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {"list_stale_events", "list_duplicate_candidates", "list_low_confidence_events"}
)


def _count_events_examined(trace: list[StepRecord]) -> int:
    """Sum the `total` from every read-tool result in `trace`."""
    total = 0
    for record in trace:
        if record.tool not in _READ_TOOLS:
            continue
        if not isinstance(record.result, dict):
            continue
        result_total = record.result.get("total")
        if isinstance(result_total, int):
            total += result_total
    return total


def _count_write_outcomes(trace: list[StepRecord]) -> dict[str, int]:
    """Return per-write-tool success counts across the trace.

    `merge_events` counts once per archived id (matches `llm_decisions`).
    Failed writes and dry-run returns don't count.
    """
    counts = {"archive": 0, "merge": 0, "update_category": 0}
    for record in trace:
        if not isinstance(record.result, dict):
            continue
        if record.result.get("status") != "ok":
            continue
        if record.tool == "archive_event":
            counts["archive"] += 1
        elif record.tool == "merge_events":
            archived = record.result.get("archived_event_ids", [])
            if isinstance(archived, list):
                counts["merge"] += len(archived)
        elif record.tool == "update_event_category":
            counts["update_category"] += 1
    return counts


def _collect_write_errors(trace: list[StepRecord]) -> list[str]:
    """Collect `error_type: message` strings for every failed write-tool call."""
    errors: list[str] = []
    for record in trace:
        if record.tool not in {"archive_event", "merge_events", "update_event_category"}:
            continue
        if not isinstance(record.result, dict):
            continue
        error_type = record.result.get("error_type")
        if error_type is None:
            continue
        message = record.result.get("message", "")
        errors.append(format_stored_text(f"{error_type}: {message}", RATIONALE_CAP))
    return errors
