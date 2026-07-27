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
from typing import Any

from agentlib.core import CHEAP
from planazo.agents.loop import LoopResult, run_loop
from planazo.memory.api import build_memory_tools
from planazo.memory.rules import load_rules
from planazo.storage import dao, db
from tools import tools as calendar_tools
from tools.schema import schema_for


def _preferences_text(user_id: int) -> str:
    """Render `user_id`'s stored preference rows as push context.

    An identity with no rows yet says so explicitly rather than rendering an
    empty heading the model has to interpret.

    Every key and value goes in as a quoted literal (`!r`), which is what keeps
    this text structural: `repr` escapes every non-printable character, line
    separators included, so one row is always exactly one line, and a stored
    value carrying a marker like `SYSTEM:` stays visibly inside quotes instead
    of reading as a heading the system message declared. `PreferenceRecord`
    rejects a multi-line value at the write boundary; this rendering holds the
    same line whatever a row on disk contains.
    """
    conn = db.connect()
    try:
        rows = dao.get_preferences(conn, user_id)
    finally:
        conn.close()
    if not rows:
        return "User preferences: none saved yet"
    return "User preferences:\n" + "\n".join(f"- {row.key!r}: {row.value!r}" for row in rows)


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
    `save_memory`, `retrieve_notes`, `save_note`), each bound to that identity
    by a closure. `user_id` is never a tool parameter, so no prompt and no
    tool-call argument can point them at another user's private facts.

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
      call as it fires; omit for no trace.
    - `gate` - an `ApprovalGate` requiring approval before any tool call whose
      name is in its set is dispatched; omit to dispatch every tool call
      without an approval prompt.
    """
    tool_schemas: list[dict[str, Any]] = [schema_for(dao.search_events)]  # Any: see schema_for
    registry: dict[str, Callable[..., dict[str, object]]] = {"search_events": dao.search_events}

    user_id: int | None = run_context.get("user_id")
    if user_id is not None:
        memory_schemas, memory_registry = build_memory_tools(user_id)
        tool_schemas = tool_schemas + memory_schemas
        registry = {**registry, **memory_registry}
    if run_context.get("calendar_enabled", False):
        tool_schemas = tool_schemas + calendar_tools.TOOL_SCHEMAS
        registry = {**registry, **calendar_tools.TOOL_REGISTRY}

    system_text = load_rules()
    if user_id is not None:
        preferences = _preferences_text(user_id)
        system_text = f"{system_text}\n\n{preferences}" if system_text else preferences

    return run_loop(
        user_message=user_message,
        tools=tool_schemas,
        registry=registry,
        model=run_context.get("model", CHEAP),
        max_steps=run_context.get("max_steps", 8),
        max_output_tokens=run_context.get("max_output_tokens"),
        on_step=run_context.get("on_step"),
        gate=run_context.get("gate"),
        # No rules on disk and no identity leaves nothing to push, and an empty
        # system message is worse than none at all.
        system=system_text or None,
    )
