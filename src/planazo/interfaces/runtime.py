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
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class LoopResult:
    """The outcome of one agent-loop run.

    Duplicated shape from `planazo.agents.loop.LoopResult` so consumers can
    accept a `LoopResult` from any conforming runtime without importing the
    concrete implementation. When a second runtime lands, the interface stays
    the invariant seam.
    """

    answer: str | None
    steps: int
    stopped: Literal["answered", "truncated", "max_steps"]


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
        on_step: Callable[..., None] | None = None,
        gate: ApprovalGate | None = None,
        system: str | None = None,
    ) -> LoopResult: ...
