"""The agent-runtime kernel contract.

Swap axis: the hand-rolled `run_loop` in `planazo.agents.loop` today; a
LangChain / LangGraph / other framework wrapper later. Consumers
(`planazo.agents.event_agent.run_once`) accept this Protocol; the concrete
implementation registers by import.

The Protocol names only the shape the composition root actually depends on
today. Additions land as downstream tickets need them; a change here is a
compatibility-surface change (rule 6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a loop run or composition-root pre-run failure.

    This is the runtime seam shared by its concrete implementation and
    consumers. `run_loop` itself creates only its existing terminal states;
    the composition root may return `preference_read_error` before a model
    call.
    """

    answer: str | None
    steps: int
    stopped: Literal["answered", "truncated", "max_steps", "preference_read_error"]


@dataclass(frozen=True)
class StepRecord:
    """One tool dispatch observed during an agent-loop run."""

    step: int
    tool: str
    arguments: dict[str, Any]
    result: Any


class ApprovalGate(Protocol):
    """The structural contract a runtime consults before dispatching a gated tool.

    Concrete implementation: `planazo.approval.gate.ApprovalGate`. A future
    non-domain caller (a WhatsApp surface's approve callback, a CI-injected
    fake) conforms without importing the domain class.
    """

    tool_names: frozenset[str]
    approve: Callable[[str, dict[str, Any]], bool]


class AgentLoop(Protocol):
    """A generic observe → reason → act → verify runtime.

    Consumed by `event_agent.run_once`. `run_loop` in `planazo.agents.loop`
    satisfies this today; a future LangGraph wrapper conforms structurally
    by exposing the same call shape.
    """

    def __call__(
        self,
        user_message: str,
        tools: list[dict[str, Any]],
        registry: dict[str, Callable[..., Any]],
        *,
        model: str,
        max_steps: int = 8,
        max_output_tokens: int | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        gate: ApprovalGate | None = None,
        system: str | None = None,
        on_tool_output: Callable[[StepRecord], list[dict[str, Any]] | None] | None = None,
    ) -> LoopResult: ...


if TYPE_CHECKING:
    from planazo.agents import loop as concrete_loop

    def _runtime_loop_adapter(
        user_message: str,
        tools: list[dict[str, Any]],
        registry: dict[str, Callable[..., Any]],
        *,
        model: str,
        max_steps: int = 8,
        max_output_tokens: int | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        gate: ApprovalGate | None = None,
        system: str | None = None,
        on_tool_output: Callable[[StepRecord], list[dict[str, Any]] | None] | None = None,
    ) -> LoopResult:
        def to_runtime_step(record: concrete_loop.StepRecord) -> StepRecord:
            return StepRecord(
                step=record.step,
                tool=record.tool,
                arguments=record.arguments,
                result=record.result,
            )

        def forward_step(record: concrete_loop.StepRecord) -> None:
            if on_step is not None:
                on_step(to_runtime_step(record))

        def forward_tool_output(
            record: concrete_loop.StepRecord,
        ) -> list[dict[str, Any]] | None:
            if on_tool_output is None:
                return None
            return on_tool_output(to_runtime_step(record))

        result = concrete_loop.run_loop(
            user_message,
            tools,
            registry,
            model=model,
            max_steps=max_steps,
            max_output_tokens=max_output_tokens,
            on_step=forward_step if on_step is not None else None,
            gate=gate,
            system=system,
            on_tool_output=forward_tool_output if on_tool_output is not None else None,
        )
        return LoopResult(answer=result.answer, steps=result.steps, stopped=result.stopped)

    agent_loop_conformance: AgentLoop = _runtime_loop_adapter
