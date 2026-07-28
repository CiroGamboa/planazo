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
from functools import wraps
from typing import Any

from pydantic import TypeAdapter, ValidationError

from agentlib.core import CHEAP
from planazo.agents.loop import LoopResult, StepRecord, run_loop
from planazo.catalog import Event, filter_events_for_intent
from planazo.catalog import search_events as catalog_search_events
from planazo.identity import PreferenceReadResult, get_preferences
from planazo.memory.api import build_memory_tools
from planazo.memory.rules import load_rules
from planazo.monitor.logging import RunStepLogger
from planazo.query.models import SearchIntent
from planazo.storage import db
from tools import tools as calendar_tools
from tools.schema import schema_for

PREFERENCE_PUSH_CAP = 1_200
PREFERENCE_OMISSION_MARKER = "- [additional preferences omitted]"
PREFERENCE_READ_ERROR_ANSWER = "Preferences could not be loaded safely; no model request was made."
MISSING_SEARCH_ORIGIN_ANSWER = (
    "A trusted location is required before applying a radius; no model request was made."
)


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


def run_once(
    user_message: str, *, intent: SearchIntent | None = None, **run_context: Any
) -> LoopResult:
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
    if intent is not None:
        preflight = filter_events_for_intent((), intent)
        if preflight.error_type == "missing_search_origin":
            return LoopResult(
                answer=MISSING_SEARCH_ORIGIN_ANSWER,
                steps=0,
                stopped="missing_search_origin",
            )

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

    @wraps(catalog_search_events)
    def search_events(
        category: str = "", city: str = "", start_after: str = "", max_results: int = 20
    ) -> dict[str, object]:
        result = catalog_search_events(category, city, start_after, max_results)
        if "error_type" in result:
            return result
        try:
            events = TypeAdapter(list[Event]).validate_python(result["events"])
        except ValidationError as exc:
            return {"error_type": "invalid_event_data", "message": str(exc)}
        if intent is None:
            return {
                "events": [event.model_dump(mode="json") for event in events],
                "total": len(events),
            }
        filtered = filter_events_for_intent(events, intent)
        if filtered.error_type is not None:
            return {"error_type": filtered.error_type}
        return {
            "events": [event.model_dump(mode="json") for event in filtered.events],
            "total": len(filtered.events),
        }

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
    logger: RunStepLogger | None = None
    if run_context.get("record_runs", True):
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
    if logger is not None:
        logger.complete(result)
    return result
