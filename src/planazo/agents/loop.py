"""The generic observe-reason-act-verify tool-calling loop.

Drives `agentlib.tools.call` across as many turns as the model needs,
dispatching every tool call it requests through a caller-supplied registry
and feeding each result back per the Responses API's `function_call_output`
contract. Stops the instant a turn comes back with no tool calls — reporting
whether that final answer was complete (`"answered"`) or cut off by the
output cap (`"truncated"`) — or once a caller-supplied step cap is hit
(`"max_steps"`), whichever comes first.

Callers can supply an `ApprovalGate` (from `planazo.approval.gate`) to
require explicit approval before any tool call whose name is in the gate's
`tool_names` is dispatched; a declined call is not run and a declined marker
is fed back to the model as that call's tool output. An optional `system`
string opens the run as a system message ahead of the user's; an optional
`max_output_tokens` caps per-turn output length by forwarding to
`agentlib.tools.call`.

A tool call that fails — an unregistered name, a tool that raises, or a
result the loop cannot serialize to feed back — is turned into a labeled
`tool_failure_result` marker rather than crashing the loop; the marker is
recorded in the trace and fed back to the model as that call's tool output.

Completely opaque to what any given tool does: `tools`/`registry` are both
supplied by the caller, so swapping in a different tool set requires zero
changes here. This module deliberately holds no `planazo.` imports so a
future runtime-kernel consolidation can move it under a shared kernel without
dragging domain code along.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from agentlib.tools import call


class ApprovalGate(Protocol):
    """The structural approval callback contract used by `run_loop`.

    Concrete implementation: `planazo.approval.gate.ApprovalGate`. This local
    Protocol keeps the generic runtime independent of Planazo package imports.
    """

    tool_names: frozenset[str]
    approve: Callable[[str, dict[str, Any]], bool]


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a loop run or composition-root pre-run failure.

    `run_loop` itself creates only `answered`, `truncated`, and `max_steps`.
    A composition root may return `preference_read_error` before a model call;
    `answer` is `None` only for `max_steps`.
    """

    answer: str | None
    steps: int
    stopped: Literal[
        "answered", "truncated", "max_steps", "preference_read_error", "missing_search_origin"
    ]


@dataclass(frozen=True)
class StepRecord:
    """One tool dispatch observed during an agent-loop run."""

    step: int
    tool: str
    arguments: dict[str, Any]
    result: Any


DECLINED_RESULT: Final[dict[str, object]] = {
    "declined": True,
    "reason": "user_declined_approval",
}


def tool_failure_result(error: str) -> dict[str, object]:
    """Build the marker fed back when a tool call fails.

    The stable shape is the contract: a JSON-serializable dict carrying
    `"tool_failed": True` and a human/model-readable `"error"` string. Callers
    pass an already-stringified `error`, so serializing the marker never
    re-crashes the loop.
    """
    return {"tool_failed": True, "error": error}


def run_loop(
    user_message: str,
    tools: list[dict[str, Any]],  # Any: tool schema dicts, forwarded to call(tools=...) as-is
    # Any: tool functions have arbitrary signatures/return types, dispatched dynamically by name
    registry: dict[str, Callable[..., Any]],
    *,
    model: str,
    max_steps: int = 8,
    max_output_tokens: int | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
    gate: ApprovalGate | None = None,
    system: str | None = None,
    # Any: injected messages are Responses-API message dicts — shape varies by
    # role/type (input_text, input_image, ...), no single fixed schema.
    on_tool_output: Callable[[StepRecord], list[dict[str, Any]] | None] | None = None,
) -> LoopResult:
    """Drive `call()` across turns until the model answers or `max_steps` is hit.

    Each iteration counts as one step, including the final turn that produces
    the answer — so `steps` is exactly the number of `call()` invocations
    made, never `max_steps + 1`. A turn with no tool calls ends the loop
    immediately, whatever the step count: its text is returned as `answer`
    with `stopped="answered"` when the turn completed, or `stopped="truncated"`
    when the turn's own output was cut off by the output cap (`answer` still
    carries the partial text). Reaching `max_steps` without an answering turn
    ends the loop with `answer=None` and `stopped="max_steps"` rather than
    truncating silently.

    A failed tool call is caught at one point and turned into a labeled
    `tool_failure_result` marker instead of crashing the run: an unregistered
    name surfaces as `"unknown tool: <name>"`, a tool that raises surfaces as
    `"<ExcType>: <message>"`, and a result the loop cannot serialize to feed
    back surfaces with the serialization error. The marker is what the
    `StepRecord` carries and what is fed back to the model as that call's
    `function_call_output`; the loop then proceeds to the next tool call or
    turn exactly as on success.

    If `on_step` is supplied, it is called with a `StepRecord` for each tool
    call the instant that call's result is computed, carrying the current step
    number (the first turn's calls report `step=1`). Default `None` fires no
    observer and is behaviourally identical to omitting it.

    If `gate` is supplied, every tool call whose name is in `gate.tool_names`
    routes through `gate.approve(name, arguments)` before dispatch. When the
    approver returns True the tool runs unchanged; when it returns False the
    tool is skipped, `DECLINED_RESULT` is emitted as that call's `StepRecord`
    result and fed back to the model as its `function_call_output`. Tools not
    named in the gate — and every tool call when `gate` is `None` — dispatch
    exactly as they do without a gate.

    If `system` is supplied, it is prepended once as a `{"role": "system"}`
    message ahead of the user's message, so it is the run's push context: it
    is sent on every turn and no tool result is ever appended to it. The
    default (`None`) opens the run with the user's message alone.

    If `max_output_tokens` is supplied, it is forwarded to every
    `agentlib.tools.call` invocation on this run, capping per-turn output.
    The default (`None`) leaves `agentlib`'s own cap in effect.

    If `on_tool_output` is supplied, it is called after each successfully-
    dispatched tool call — once the call's `function_call_output` message has
    been appended and `on_step` (if any) has observed the step — with that
    same `StepRecord`. When the callable returns a non-empty list, each dict
    in the list is appended to `messages` in order, immediately after the
    `function_call_output` for that call and before the loop advances to the
    next tool call or the next `call()` turn. A `None` or empty-list return
    is a no-op; the default (`None`) is behaviourally identical to omitting
    the hook. Failed tool calls (unregistered name, tool that raised,
    unserializable result) do NOT trigger the hook — the injected-message
    shape is a caller-facing seam that only makes sense on happy dispatches.
    If the hook itself raises, the exception propagates out of `run_loop`
    unchanged: no `tool_failure_result` wrap, no swallow — the hook is a
    caller-side seam, not a third-party surface, and a raising hook is a bug
    in the caller's own code (matching `on_step`'s own uncaught behaviour).
    """
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValueError("max_output_tokens must be >= 1")

    # Any: Responses-API message dicts — shape varies by role/type (user turn,
    # function_call, function_call_output, ...), no single fixed schema.
    messages: list[dict[str, Any]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_message})

    call_kwargs: dict[str, Any] = {}  # Any: passthrough kwargs for agentlib.tools.call
    if max_output_tokens is not None:
        call_kwargs["max_output_tokens"] = max_output_tokens

    steps = 0
    while True:
        steps += 1
        result = call(messages=messages, model=model, tools=tools, **call_kwargs)

        if not result.tool_calls:
            stopped: Literal["answered", "truncated"] = (
                "truncated" if result.truncated else "answered"
            )
            return LoopResult(answer=result.text, steps=steps, stopped=stopped)

        messages = messages + result.output_items
        for tool_call in result.tool_calls:
            name = tool_call["name"]
            arguments = tool_call["arguments"]
            # Any: a tool's return value has no fixed shape — it's whatever
            # that tool's registered callable happens to produce, the
            # `DECLINED_RESULT` marker when a gated call is refused, or a
            # `tool_failure_result` marker when the call fails.
            output: Any
            dispatched = False
            if gate is not None and name in gate.tool_names and not gate.approve(name, arguments):
                output = DECLINED_RESULT
                serialized = json.dumps({"result": output})
            elif name not in registry:
                output = tool_failure_result(f"unknown tool: {name}")
                serialized = json.dumps({"result": output})
            else:
                # One catch-and-branch point covers both a raising tool and a
                # result the loop cannot feed back: dispatch and its feed-back
                # serialization run together so either failure yields the same
                # labeled marker (itself always serializable).
                try:
                    output = registry[name](**arguments)
                    serialized = json.dumps({"result": output})
                    dispatched = True
                except Exception as exc:
                    output = tool_failure_result(f"{type(exc).__name__}: {exc}")
                    serialized = json.dumps({"result": output})
            record = StepRecord(
                step=steps,
                tool=name,
                arguments=arguments,
                result=output,
            )
            if on_step is not None:
                on_step(record)
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call["call_id"],
                    "output": serialized,
                }
            )
            # `on_tool_output` fires only on a happy dispatch (in the registry,
            # ran, serialized) — declined and failed calls skip the hook so a
            # caller-side injection is never confused with a `function_call_output`
            # carrying a `tool_failure_result` marker. A raising hook is a
            # caller-side bug: let it propagate unchanged.
            if dispatched and on_tool_output is not None:
                injected = on_tool_output(record)
                if injected:
                    messages.extend(injected)

        if steps >= max_steps:
            return LoopResult(answer=None, steps=steps, stopped="max_steps")
