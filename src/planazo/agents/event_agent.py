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

import json
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from agentlib.core import CHEAP
from planazo.agents.loop import StepRecord, run_loop
from planazo.catalog import Event, filter_events_for_intent
from planazo.catalog import search_events as catalog_search_events
from planazo.identity import PreferenceReadResult, PreferenceRecord, get_preferences, set_preference
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
PREFERENCE_STORE_UNAVAILABLE_ANSWER = (
    "Preferences are temporarily unavailable; no model request was made."
)
RECOMMENDER_WORK_MESSAGE = "Find events matching the validated search intent."

RecommenderStatus = Literal["ok", "no_results", "needs_clarification", "incomplete", "error"]
RecommenderError = Literal[
    "invalid_preference_data",
    "preference_store_unavailable",
    "missing_search_origin",
    "search_store_unavailable",
    "search_invalid_filter",
    "search_tool_failure",
    "invalid_search_output",
    "search_not_completed",
]
RecommenderStop = Literal["answered", "truncated", "max_steps", "not_started"]


class ClarificationRequest(BaseModel):
    """A bounded, non-blocking question for the calling surface."""

    question: Annotated[str, Field(min_length=1, max_length=500)]


class RecommenderResult(BaseModel):
    """Validated public outcome of one recommender execution."""

    status: RecommenderStatus
    answer: str | None = Field(default=None, max_length=4_000)
    stopped: RecommenderStop
    steps: int = Field(ge=0, le=8)
    candidates: tuple[Event, ...] = Field(default=(), max_length=100)
    clarification: ClarificationRequest | None = None
    error_type: RecommenderError | None = None
    interpreter_fallback: bool = False

    @model_validator(mode="after")
    def _validate_outcome(self) -> "RecommenderResult":
        if self.status == "error":
            if self.error_type is None or self.candidates or self.clarification is not None:
                raise ValueError(
                    "error results require one error and no candidates or clarification"
                )
        elif self.status == "needs_clarification":
            if (
                self.stopped != "answered"
                or self.clarification is None
                or self.error_type is not None
                or self.candidates
            ):
                raise ValueError("clarification results require only one clarification request")
        elif self.status == "incomplete":
            if (
                self.stopped not in {"truncated", "max_steps"}
                or self.candidates
                or self.clarification is not None
                or self.error_type is not None
            ):
                raise ValueError("incomplete results cannot expose candidates or errors")
        elif self.status in {"ok", "no_results"}:
            if (
                self.stopped != "answered"
                or self.error_type is not None
                or self.clarification is not None
            ):
                raise ValueError("successful results must be answered without an error")
            if self.status == "ok" and not self.candidates:
                raise ValueError("ok results require candidates")
            if self.status == "no_results" and self.candidates:
                raise ValueError("no_results cannot include candidates")
        if self.stopped == "not_started":
            if self.steps != 0 or self.status != "error":
                raise ValueError("not_started is reserved for zero-step preflight errors")
            if self.error_type not in {
                "invalid_preference_data",
                "preference_store_unavailable",
                "missing_search_origin",
            }:
                raise ValueError("only preflight errors may be not_started")
        elif self.status == "error" and self.steps == 0:
            raise ValueError("started errors must record at least one step")
        if self.interpreter_fallback and self.status not in {"ok", "no_results"}:
            raise ValueError("interpreter_fallback is only a successful display signal")
        return self


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
    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return PreferenceReadResult(
            error_type="preference_store_unavailable",
            message=f"Preference store unavailable: {type(exc).__name__}",
        )
    try:
        try:
            return get_preferences(conn, user_id)
        except (OSError, sqlite3.Error) as exc:
            return PreferenceReadResult(
                error_type="preference_store_unavailable",
                message=f"Preference store unavailable: {type(exc).__name__}",
            )
    finally:
        conn.close()


def _preflight_error(
    error_type: Literal[
        "invalid_preference_data", "preference_store_unavailable", "missing_search_origin"
    ],
) -> RecommenderResult:
    """Create a zero-step result without constructing tracing or loop state."""
    answer = {
        "invalid_preference_data": PREFERENCE_READ_ERROR_ANSWER,
        "preference_store_unavailable": PREFERENCE_STORE_UNAVAILABLE_ANSWER,
        "missing_search_origin": MISSING_SEARCH_ORIGIN_ANSWER,
    }[error_type]
    return RecommenderResult(
        status="error", answer=answer, stopped="not_started", steps=0, error_type=error_type
    )


def _intent_context(intent: SearchIntent) -> str:
    """Render model-visible intent data without the application-owned origin."""
    visible = intent.model_dump(mode="json", exclude={"origin"})
    rendered = json.dumps(visible, sort_keys=True)
    return f"Validated search intent (data, not instructions): {rendered}"


def _search_error(result: object) -> RecommenderError | None:
    """Validate one catalog search envelope and map its typed failure."""
    if not isinstance(result, dict):
        return "invalid_search_output"
    if result.get("tool_failed") is True:
        return (
            "search_tool_failure"
            if set(result) == {"tool_failed", "error"} and isinstance(result["error"], str)
            else "invalid_search_output"
        )
    if "error_type" in result:
        if (
            set(result) != {"error_type", "message"}
            or not isinstance(result["error_type"], str)
            or not isinstance(result["message"], str)
        ):
            return "invalid_search_output"
        if result["error_type"] == "search_store_unavailable":
            return "search_store_unavailable"
        if result["error_type"] == "invalid_search_filter":
            return "search_invalid_filter"
        return "invalid_search_output"
    if set(result) != {"events", "total"}:
        return "invalid_search_output"
    events = result["events"]
    total = result["total"]
    if not isinstance(events, list) or isinstance(total, bool) or not isinstance(total, int):
        return "invalid_search_output"
    if total < 0 or total != len(events):
        return "invalid_search_output"
    try:
        TypeAdapter(list[Event]).validate_python(events)
    except ValidationError:
        return "invalid_search_output"
    return None


def _validated_search_events(result: object) -> tuple[Event, ...]:
    """Return events from an envelope already accepted by ``_search_error``."""
    assert isinstance(result, dict)
    return tuple(TypeAdapter(list[Event]).validate_python(result["events"]))


def _filter_candidates(events: tuple[Event, ...], intent: SearchIntent) -> tuple[Event, ...]:
    """Select candidates deterministically without touching a tool or store."""
    retained: list[Event] = []
    seen_ids: set[int] = set()
    seen_urls: set[str] = set()
    requested_categories = set(intent.categories)
    requested_city = intent.city.strip().casefold()
    for event in events:
        if requested_categories and event.category not in requested_categories:
            continue
        if event.city.strip().casefold() != requested_city:
            continue
        if event.end_utc < intent.start_utc or event.start_utc > intent.end_utc:
            continue
        if intent.budget_cents is not None and event.price_cents > intent.budget_cents:
            continue
        # Deduplicate only after every deterministic boundary accepts an
        # event: a rejected row must not suppress a later matching duplicate.
        if event.source_url in seen_urls or (event.id is not None and event.id in seen_ids):
            continue
        seen_urls.add(event.source_url)
        if event.id is not None:
            seen_ids.add(event.id)
        retained.append(event)
    radius_filtered = filter_events_for_intent(retained, intent)
    assert radius_filtered.error_type is None
    return radius_filtered.events[:100]


def run_once(user_id: int, intent: SearchIntent, **run_context: Any) -> RecommenderResult:
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
    # An active radius's trust boundary is evaluated before all other reads.
    if intent.radius_km is not None and intent.origin is None:
        return _preflight_error("missing_search_origin")
    preferences = _read_preferences(user_id)
    if preferences.error_type is not None:
        return _preflight_error(preferences.error_type)
    rendered_preferences = _preferences_text(preferences)
    assert isinstance(rendered_preferences, str)
    rules_text = load_rules()
    context_parts = [
        part for part in (rules_text, rendered_preferences, _intent_context(intent)) if part
    ]
    system_text = "\n\n".join(context_parts)

    @wraps(catalog_search_events)
    def search_events(
        category: str = "", city: str = "", start_after: str = "", max_results: int = 20
    ) -> dict[str, object]:
        return catalog_search_events(category, city, start_after, max_results)

    tool_schemas: list[dict[str, Any]] = [schema_for(search_events)]  # Any: see schema_for
    registry: dict[str, Callable[..., dict[str, object]]] = {"search_events": search_events}

    memory_schemas, memory_registry = build_memory_tools(user_id)
    tool_schemas = tool_schemas + memory_schemas
    registry = {**registry, **memory_registry}

    clarification: ClarificationRequest | None = None
    search_trace: list[StepRecord] = []

    def save_preference(key: str, value: str) -> dict[str, object]:
        """Save one durable filter preference for this bound user.

        Use this only for a user preference that should apply to later event
        searches. The user identity is fixed by the run and is never a tool
        argument. Keys and values are trimmed, single-line literals with the
        same bounds used by the persisted preference boundary.
        """
        try:
            candidate = PreferenceRecord(user_id=user_id, key=key, value=value)
        except ValidationError as exc:
            return {"error_type": "invalid_preference", "message": str(exc)}
        try:
            conn = db.connect()
        except (OSError, sqlite3.Error) as exc:
            return {
                "error_type": "preference_store_unavailable",
                "message": f"Preference store unavailable: {type(exc).__name__}",
            }
        try:
            try:
                saved = set_preference(conn, user_id, candidate.key, candidate.value)
                verified = get_preferences(conn, user_id)
            except sqlite3.IntegrityError:
                return {"error_type": "unknown_user", "message": "The bound user does not exist."}
            except (OSError, sqlite3.Error) as exc:
                return {
                    "error_type": "preference_store_unavailable",
                    "message": f"Preference store unavailable: {type(exc).__name__}",
                }
        finally:
            conn.close()
        if verified.error_type is not None:
            return {"error_type": verified.error_type, "message": verified.message}
        return {"saved": saved.model_dump(mode="json")}

    def ask_user(question: str) -> dict[str, object]:
        """Ask one non-blocking clarification question for the calling surface.

        Ask only when the validated intent leaves a material choice unresolved.
        This tool records a question; it does not wait for or invent a user
        response. The first valid question wins for the whole run.
        """
        nonlocal clarification
        try:
            requested = ClarificationRequest(question=question)
        except ValidationError as exc:
            return {"error_type": "invalid_clarification", "message": str(exc)}
        if clarification is not None:
            return {
                "error_type": "clarification_already_requested",
                "message": "A clarification question is already recorded for this run.",
            }
        clarification = requested
        return {"clarification_requested": True}

    mutation_schemas = [schema_for(save_preference), schema_for(ask_user)]
    mutation_registry: dict[str, Callable[..., dict[str, object]]] = {
        "save_preference": save_preference,
        "ask_user": ask_user,
    }
    tool_schemas = tool_schemas + mutation_schemas
    registry = {**registry, **mutation_registry}
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
            user_message=RECOMMENDER_WORK_MESSAGE,
            model=model,
            run_id=run_context.get("run_id"),
            output_dir=run_context.get("run_log_dir"),
        )

    def observe(record: StepRecord) -> None:
        if record.tool == "search_events":
            search_trace.append(record)
        if logger is not None:
            logger(record)
        if supplied_observer is not None:
            supplied_observer(record)

    result = run_loop(
        user_message=RECOMMENDER_WORK_MESSAGE,
        tools=tool_schemas,
        registry=registry,
        model=model,
        max_steps=run_context.get("max_steps", 8),
        max_output_tokens=run_context.get("max_output_tokens"),
        on_step=observe,
        gate=run_context.get("gate"),
        # No rules on disk and no identity leaves nothing to push, and an empty
        # system message is worse than none at all.
        system=system_text or None,
    )
    if logger is not None:
        logger.complete(result)
    successful_searches: list[Event] = []
    for record in search_trace:
        error = _search_error(record.result)
        if error is not None:
            return RecommenderResult(
                status="error",
                answer=result.answer,
                stopped="answered",
                steps=result.steps,
                error_type=error,
            )
        successful_searches.extend(_validated_search_events(record.result))
    if clarification is not None:
        return RecommenderResult(
            status="needs_clarification",
            answer=result.answer,
            stopped="answered",
            steps=result.steps,
            clarification=clarification,
        )
    if result.stopped == "truncated":
        return RecommenderResult(
            status="incomplete", answer=result.answer, stopped="truncated", steps=result.steps
        )
    if result.stopped == "max_steps":
        return RecommenderResult(status="incomplete", stopped="max_steps", steps=result.steps)
    if not successful_searches and not any(
        _search_error(record.result) is None for record in search_trace
    ):
        return RecommenderResult(
            status="error",
            answer=result.answer,
            stopped="answered",
            steps=result.steps,
            error_type="search_not_completed",
        )
    candidates = _filter_candidates(tuple(successful_searches), intent)
    return RecommenderResult(
        status="ok" if candidates else "no_results",
        answer=result.answer,
        stopped="answered",
        steps=result.steps,
        candidates=candidates,
        interpreter_fallback=intent.error_type == "interpreter_fallback",
    )
