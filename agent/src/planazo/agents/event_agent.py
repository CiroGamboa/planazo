"""Planazo's entrypoint into the observe-reason-act-verify agent loop.

Composes the agent's tool set and binds it to the generic `run_loop`, exposing
a single `run_once()` front door that runs the agent against the real LLM for
one user message. This loop *is* Planazo's agent
(`docs/PLANAZO-PROJECT-CONTEXT.md`'s observe -> reason -> act -> verify ->
repeat) — not a class-exercise stand-in next to a separate "real" pipeline.
This is the one place the tool set is bound to the loop; callers supply only
the message and any per-run options (model, step cap, per-turn output cap,
per-step observer, approval gate, calendar tools).

`tools.tools` is imported as a module, not by name, so the registry and schemas
it exports are read at call time — a caller (or a test) that replaces them sees
the replacement.
"""

from collections.abc import Callable
from typing import Any

from agentlib.core import CHEAP
from planazo.agents.loop import LoopResult, run_loop
from planazo.storage import dao
from tools import tools as calendar_tools
from tools.schema import schema_for


def run_once(user_message: str, **run_context: Any) -> LoopResult:
    """Run one observe -> reason -> act -> verify pass for `user_message`.

    The default tool set is `search_events` alone — a read-only query over the
    shared `events` table, so nothing the model can reach on a default run
    writes anything.

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
    if run_context.get("calendar_enabled", False):
        tool_schemas = tool_schemas + calendar_tools.TOOL_SCHEMAS
        registry = {**registry, **calendar_tools.TOOL_REGISTRY}

    return run_loop(
        user_message=user_message,
        tools=tool_schemas,
        registry=registry,
        model=run_context.get("model", CHEAP),
        max_steps=run_context.get("max_steps", 8),
        max_output_tokens=run_context.get("max_output_tokens"),
        on_step=run_context.get("on_step"),
        gate=run_context.get("gate"),
    )
