"""Planazo's entrypoint into the observe-reason-act-verify agent loop.

Wires the event-discovery tools (`TOOL_SCHEMAS`/`TOOL_REGISTRY`) to the
generic `run_loop`, exposing a single `run_once()` front door that runs the
agent against the real LLM for one user message. This loop *is* Planazo's
agent (`docs/PLANAZO-PROJECT-CONTEXT.md`'s observe -> reason -> act ->
verify -> repeat) — not a class-exercise stand-in next to a separate "real"
pipeline. This is the one place the tool set is bound to the loop; callers
supply only the message and any per-run options (model, step cap, per-turn
output cap, per-step observer, approval gate).
"""

from typing import Any

from agentlib.core import CHEAP
from planazo.agents.loop import LoopResult, run_loop
from tools.tools import TOOL_REGISTRY, TOOL_SCHEMAS


def run_once(user_message: str, **run_context: Any) -> LoopResult:
    """Run one observe -> reason -> act -> verify pass for `user_message`.

    Uses the event-discovery tools:
    - `save_event_candidate` is reversible (a correction is just another
      write); it does not require approval before dispatch.
    - `confirm_and_create_calendar_event` is irreversible: it puts an event
      on the user's calendar and can email other people. Callers pass an
      `ApprovalGate` covering this tool's name when they want a human to
      confirm each call; the CLI does this by default, library callers of
      `run_once` opt in via the `gate` kwarg.

    Recognised `run_context` keys, all optional:
    - `model` - the model id to run against (defaults to the pinned cheap role).
    - `max_steps` - the loop's step cap (defaults to `run_loop`'s own default).
    - `max_output_tokens` - per-turn output cap forwarded to every LLM
      call; defaults to `agentlib`'s own cap.
    - `on_step` - a per-step observer called with a `StepRecord` for each tool
      call as it fires; omit for no trace.
    - `gate` - an `ApprovalGate` requiring approval before any tool call whose
      name is in its set is dispatched; omit to dispatch every tool call
      without an approval prompt.
    """
    return run_loop(
        user_message=user_message,
        tools=TOOL_SCHEMAS,
        registry=TOOL_REGISTRY,
        model=run_context.get("model", CHEAP),
        max_steps=run_context.get("max_steps", 8),
        max_output_tokens=run_context.get("max_output_tokens"),
        on_step=run_context.get("on_step"),
        gate=run_context.get("gate"),
    )
