"""Planazo's entrypoint into the observe-reason-act-verify agent loop.

Composes the agent's tool set and binds it to the generic `run_loop`, exposing
a single `run_once()` front door that runs the agent against the real LLM for
one user message. This loop *is* Planazo's agent
(`docs/PLANAZO-PROJECT-CONTEXT.md`'s observe -> reason -> act -> verify ->
repeat) — not a class-exercise stand-in next to a separate "real" pipeline.
This is the one place the tool set is bound to the loop; callers supply only
the message and any per-run options (model, identity, step cap, per-turn output
cap, per-step observer, approval gate, calendar tools).

It is also the one place push context is assembled: the markdown rules, plus
the caller's stored preferences when an identity is supplied, become the run's
system message. Facts and notes never join them — everything the model learns
about them it pulls through a tool, so their content reaches it only as a tool
result and never in an instruction-bearing role
(`docs/adr/0004-three-store-memory-model.md`). Preference rows are the one
stored value pushed into that role, and they go in as quoted single-line
literals so no stored value can forge structure the system message did not
declare.

`tools.tools` is imported as a module, not by name, so the registry and schemas
it exports are read at call time — a caller (or a test) that replaces them sees
the replacement.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agentlib.core import CHEAP
from planazo.agents.loop import LoopResult, StepRecord, run_loop
from planazo.catalog import search_events
from planazo.identity import PreferenceReadResult, get_preferences
from planazo.memory.api import build_memory_tools
from planazo.memory.rules import load_rules
from planazo.monitor.logging import RunStepLogger
from planazo.observability import (
    FINAL_ANSWER_CAP,
    USER_QUERY_CAP,
    AgentRunLogger,
    AgentRunRecord,
    format_stored_text,
)
from planazo.storage import db
from tools import tools as calendar_tools
from tools.schema import schema_for

PREFERENCE_PUSH_CAP = 1_200
PREFERENCE_OMISSION_MARKER = "- [additional preferences omitted]"
PREFERENCE_READ_ERROR_ANSWER = "Preferences could not be loaded safely; no model request was made."


def _preferences_text(preferences: PreferenceReadResult) -> str | PreferenceReadResult:
    """Render a validated preference read as bounded, deterministic push context.

    The full section is capped at `PREFERENCE_PUSH_CAP` Unicode code points.
    Rows are whole quoted lines in repository key order; reserving the exact
    omission marker before considering a row makes every omission visible.
    Invalid persisted data is returned untouched so `run_once` can stop before
    it creates a trace or makes a model request.
    """
    if preferences.error_type is not None:
        return preferences
    if not preferences.rows:
        return "User preferences: none saved yet"

    heading = "User preferences:"
    suffix = f"\n{PREFERENCE_OMISSION_MARKER}"
    included_lines = ""
    for row in preferences.rows:
        next_line = f"\n- {row.key!r}: {row.value!r}"
        if len(heading + included_lines + next_line + suffix) > PREFERENCE_PUSH_CAP:
            return heading + included_lines + suffix
        included_lines += next_line
    return heading + included_lines


def _read_preferences(user_id: int) -> PreferenceReadResult:
    """Read one identity's preference rows through the typed repository boundary."""
    conn = db.connect()
    try:
        return get_preferences(conn, user_id)
    finally:
        conn.close()


def run_once(user_message: str, **run_context: Any) -> LoopResult:
    """Run one observe -> reason -> act -> verify pass for `user_message`.

    The run's system message is push context, assembled here before the loop
    starts: `load_rules()`'s markdown, re-read from disk every call so an
    operator changes the agent by editing a committed file, plus `user_id`'s
    stored preference rows when an identity is supplied.

    The default tool set is `search_events` alone — a read-only query over the
    shared `events` table, so nothing the model can reach on a default run
    writes anything.

    Supplying `user_id` adds the four memory tools (`retrieve_memory`,
    `save_memory`, `retrieve_notes`, `save_note`) plus `dispatch_extraction`,
    each bound to that identity by a closure. `user_id` is never a tool
    parameter, so no prompt and no tool-call argument can point them at
    another user's private facts or forge the delegator on an extraction.
    `dispatch_extraction` reaches the Extractor through a lazy import inside
    this function so `event_agent.py`'s static import graph never touches
    `planazo.sources.instagram` (ADR 0005 §Trust boundary).

    The two calendar reference tools are opt-in through `calendar_enabled`.
    When they are enabled:
    - `save_event_candidate` is reversible (a correction is just another
      write); it does not require approval before dispatch.
    - `confirm_and_create_calendar_event` is irreversible: it puts an event on
      the user's calendar and can email other people. Callers pass an
      `ApprovalGate` covering this tool's name when they want a human to
      confirm each call; the CLI does this on every run, library callers of
      `run_once` opt in via the `gate` kwarg.

    Recognised `run_context` keys, all optional:
    - `model` - the model id to run against (defaults to the pinned cheap role).
    - `user_id` - the identity the memory tools and the preferences push are
      bound to (defaults to `None`: no memory tools, rules-only push context).
    - `calendar_enabled` - add the two calendar reference tools to the run's
      tool set (defaults to `False`).
    - `max_steps` - the loop's step cap (defaults to `run_loop`'s own default).
    - `max_output_tokens` - per-turn output cap forwarded to every LLM
      call; defaults to `agentlib`'s own cap.
    - `on_step` - a per-step observer called with a `StepRecord` for each tool
      call as it fires; it is invoked after the built-in JSONL trace writer.
    - `run_id` - stable run identifier used by the trace writer; generated when omitted.
    - `run_log_dir` - optional trace output directory, useful for isolated callers/tests.
    - `record_runs` - set False only for callers that must not persist an audit trace.
    - `gate` - an `ApprovalGate` requiring approval before any tool call whose
      name is in its set is dispatched; omit to dispatch every tool call
      without an approval prompt.
    """
    user_id: int | None = run_context.get("user_id")
    system_text = load_rules()
    if user_id is not None:
        preferences = _preferences_text(_read_preferences(user_id))
        if isinstance(preferences, PreferenceReadResult):
            # This is a composition failure, not a loop terminal state: do not
            # create a monitor trace that would violate RunStep's invariants.
            return LoopResult(
                answer=PREFERENCE_READ_ERROR_ANSWER,
                steps=0,
                stopped="preference_read_error",
            )
        system_text = f"{system_text}\n\n{preferences}" if system_text else preferences

    tool_schemas: list[dict[str, Any]] = [schema_for(search_events)]  # Any: see schema_for
    registry: dict[str, Callable[..., dict[str, object]]] = {"search_events": search_events}

    if user_id is not None:
        memory_schemas, memory_registry = build_memory_tools(user_id)
        tool_schemas = tool_schemas + memory_schemas
        registry = {**registry, **memory_registry}
        # Lazy import: `planazo.extraction.tools` top-imports the Extractor,
        # which top-imports `planazo.sources.instagram`. Reaching it here
        # keeps that chain out of `event_agent.py`'s static import graph
        # (ADR 0005 §Trust boundary), verified by the AST guard in
        # `tests/test_trust_boundary.py`.
        from planazo.extraction.tools import build_dispatch_extraction

        extraction_schemas, extraction_registry = build_dispatch_extraction(user_id)
        tool_schemas = tool_schemas + extraction_schemas
        registry = {**registry, **extraction_registry}
    if run_context.get("calendar_enabled", False):
        tool_schemas = tool_schemas + calendar_tools.TOOL_SCHEMAS
        registry = {**registry, **calendar_tools.TOOL_REGISTRY}

    model = run_context.get("model", CHEAP)
    supplied_observer = run_context.get("on_step")
    record_runs = run_context.get("record_runs", True)
    logger: RunStepLogger | None = None
    if record_runs:
        logger = RunStepLogger(
            user_message=user_message,
            model=model,
            run_id=run_context.get("run_id"),
            output_dir=run_context.get("run_log_dir"),
        )

    def observe(record: StepRecord) -> None:
        if logger is not None:
            logger(record)
        if supplied_observer is not None:
            supplied_observer(record)

    observer: Callable[[StepRecord], None] | None = observe if logger or supplied_observer else None
    # Capture the loop's wall-clock boundaries around `run_loop` so the
    # `agent_runs` row's `started_at` / `ended_at` cover the full loop —
    # including tool dispatches, not just the LLM turns.
    started_at = datetime.now(UTC)
    result = run_loop(
        user_message=user_message,
        tools=tool_schemas,
        registry=registry,
        model=model,
        max_steps=run_context.get("max_steps", 8),
        max_output_tokens=run_context.get("max_output_tokens"),
        on_step=observer,
        gate=run_context.get("gate"),
        # No rules on disk and no identity leaves nothing to push, and an empty
        # system message is worse than none at all.
        system=system_text or None,
    )
    ended_at = datetime.now(UTC)
    if logger is not None:
        logger.complete(result)
        # SQLite write is gated by the same `record_runs` seam as the JSONL
        # writer above. Best-effort: `AgentRunLogger` catches every exception
        # and logs a WARNING; the Recommender's answer is the primary flow
        # and observability failures must not affect it (Rule 4).
        _record_agent_run_best_effort(
            run_id=logger.run_id,
            user_id=user_id,
            user_message=user_message,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
        )
    return result


def _record_agent_run_best_effort(
    *,
    run_id: str,
    user_id: int | None,
    user_message: str,
    result: LoopResult,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    """Build the sanitized `AgentRunRecord` and hand it to `AgentRunLogger`.

    The `preference_read_error` branch cannot reach here — `run_once`
    returns early before the loop starts, before this helper is called.
    Every other `LoopResult.stopped` value is a valid `AgentRunStopped`
    Literal, so the aggregate constructs cleanly and the logger's own
    best-effort swallow only catches genuine DB-side failures.
    """
    assert result.stopped != "preference_read_error", (
        "agent_runs records actual loop terminals; pre-run failures must not be logged"
    )
    agent_logger = AgentRunLogger(conn_factory=db.connect)
    record = AgentRunRecord(
        run_id=run_id,
        agent_kind="recommender",
        user_id=user_id,
        user_query=format_stored_text(user_message, USER_QUERY_CAP),
        final_answer=(
            format_stored_text(result.answer, FINAL_ANSWER_CAP)
            if result.answer is not None
            else None
        ),
        # `LoopResult.stopped` widens over the aggregate's Literal by one
        # branch (`preference_read_error`); the assert above narrows it out.
        stopped=result.stopped,
        steps_count=result.steps,
        started_at=started_at,
        ended_at=ended_at,
    )
    agent_logger.record(record)
