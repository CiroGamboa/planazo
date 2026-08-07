"""Pydantic models for the agent-eval harness.

These aggregates travel across the harness's four surfaces:

- ``AgentEvalCase`` and ``ExpectedToolCall`` come out of the JSONL scenario
  file — they are the ground truth the trajectory metrics score against.
- ``ToolCall`` is the shape each observed tool call takes after the MLflow
  trace adapter has walked the ``TOOL`` spans.
- ``RunResult`` is one row per (case, run_index) — carries the numeric
  metric outputs so the CLI writer can serialise it as one JSONL line.
- ``ScenarioResult`` bundles the three runs of one scenario and exposes
  the ``pass@3`` and ``pass^3`` predicates the HW4 report cites.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """One observed tool call, materialised from a ``TOOL``-typed MLflow span.

    ``arguments`` is the dict recovered from ``mlflow.spanInputs`` on the
    tool span — LangGraph's ``ToolNode`` stores the tool's positional
    arguments under their parameter names, so a key/value inspection here
    mirrors what the model actually sent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    def args_contains(self, other: dict[str, Any]) -> bool:
        """Return ``True`` iff every key/value in ``other`` matches this call.

        The comparison is subset-style: extra arguments on ``self`` are
        ignored, missing/mismatched keys on ``self`` fail the check. Values
        are compared with ``==``. An empty ``other`` always returns ``True``
        — the metric layer uses this to model "any invocation of tool X
        counts, regardless of arguments".
        """
        for key, expected_value in other.items():
            if key not in self.arguments:
                return False
            if self.arguments[key] != expected_value:
                return False
        return True


class ExpectedToolCall(BaseModel):
    """One expected tool call declared in an ``AgentEvalCase``.

    ``args_contains`` is a subset match — the harness accepts any actual
    call whose arguments cover the declared keys with equal values. The
    scenarios file leaves it ``{}`` when the metric should reward any
    invocation of the named tool.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    args_contains: dict[str, Any] = Field(default_factory=dict)


class AgentEvalCase(BaseModel):
    """One scenario in ``data/eval/agent_scenarios.jsonl``.

    ``expected_tools`` is a list because the harness may declare zero,
    one, or multiple expected calls per scenario — an empty list is a
    valid signal that no tool call is expected (e.g. a preflight abort).
    ``expected_outcome`` is a free-text description the goal-completion
    judge scores the model's final answer against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    expected_tools: list[ExpectedToolCall] = Field(default_factory=list)
    expected_outcome: str = Field(min_length=1)
    notes: str = ""


class RunResult(BaseModel):
    """One run of one scenario — the atomic row the JSONL writer emits.

    All metric fields are optional so the harness can persist a
    partially-scored row: the tool-selection and trajectory scores are
    computed inline (they need only the trace), while goal_completion is
    left ``None`` here — the batch scorer runner (Stage 2 wiring) fills it
    in later against the same trace.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    run_index: int = Field(ge=0)
    status: str = Field(min_length=1)
    answer: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    trace_id: str | None = None
    error_type: str | None = None
    tool_selection: float | None = None
    traj_precision: float | None = None
    traj_recall: float | None = None
    goal_completion: float | None = None


class ScenarioResult(BaseModel):
    """The three runs of one scenario, plus the reliability roll-ups.

    ``pass@3`` and ``pass^3`` are computed on-demand from ``runs`` — the
    class carries no denormalised copy so a downstream consumer that mutates
    ``runs`` never sees stale roll-ups.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    runs: list[RunResult] = Field(default_factory=list)

    def _tool_selection_at(self, run: RunResult) -> float:
        """Read one run's tool-selection score, defaulting a ``None`` to ``0.0``.

        A ``None`` score means the run finished without a scored trajectory
        (e.g. the harness aborted the run early). It is neither a pass nor
        a fail; treating it as ``0.0`` for the pass predicate keeps ``pass@3``
        conservative — a scenario needs a real observed pass to be marked
        reliable.
        """
        return run.tool_selection if run.tool_selection is not None else 0.0

    def pass_at_3(self, threshold: float = 0.5) -> bool:
        """``True`` iff at least one of the runs scored ``tool_selection >= threshold``."""
        if not self.runs:
            return False
        return any(self._tool_selection_at(run) >= threshold for run in self.runs)

    def pass_cubed(self, threshold: float = 0.5) -> bool:
        """``True`` iff **every** run scored ``tool_selection >= threshold``.

        Named ``pass^3`` for the 3-run harness; the property holds for any
        run count, so a smoke run with ``runs=1`` still reports a meaningful
        value even though the reliability signal is thin.
        """
        if not self.runs:
            return False
        return all(self._tool_selection_at(run) >= threshold for run in self.runs)


__all__ = [
    "AgentEvalCase",
    "ExpectedToolCall",
    "RunResult",
    "ScenarioResult",
    "ToolCall",
]
