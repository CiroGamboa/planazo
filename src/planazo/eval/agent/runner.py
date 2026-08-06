"""3-run reliability harness for one ``AgentEvalCase``.

For each run the harness threads:

- the raw scenario text through ``planazo.query.interpret`` — the same
  interpreter the CLI and the bot use — to build a ``SearchIntent``,
- ``request_origin="eval"`` and ``eval_case_id="{case_id}-run-{k}"`` into
  ``run_context`` so the MLflow trace this call produces is tagged as
  an eval invocation the join key can later dispatch on,
- an optional ``temperature`` override into ``run_context`` — the
  Recommender's chat-model factory picks it up per HW4 Part 1's
  reliability protocol.

The runner scores each run's tool-selection + trajectory precision +
recall inline (metrics are pure over the trace); goal completion is left
for the batch scorer runner (Stage 2 wiring). Latency is measured with
``time.perf_counter`` around ``run_once``.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

import time
from typing import Any

import mlflow

from agentlib.core import CHEAP
from planazo.agents.event_agent import RecommenderResult, run_once
from planazo.eval.agent.adapters import trace_to_tool_calls
from planazo.eval.agent.metrics import (
    tool_selection_accuracy,
    trajectory_precision_recall,
)
from planazo.eval.agent.models import (
    AgentEvalCase,
    RunResult,
    ScenarioResult,
    ToolCall,
)
from planazo.query.interpreter import interpret
from planazo.query.models import SearchIntent


def _intent_for(case: AgentEvalCase) -> SearchIntent:
    """Route the scenario's raw input through the same interpreter the CLI uses.

    A ``ChatRoute`` (small-talk / meta-question routing) is downgraded to
    the interpreter's degraded search fallback so the runner always has a
    ``SearchIntent`` to call ``run_once`` with — matches the compatibility
    surface the clarification-answer path uses (ADR 0016 + ADR 0020 §D5).
    """
    routed = interpret(case.input)
    if routed.kind == "search":
        return routed.intent
    from planazo.query.interpreter import _fallback_search_route

    return _fallback_search_route().intent


def _fetch_trace_id() -> str | None:
    """Return the most recently completed trace's id, or ``None`` if unavailable.

    Uses ``mlflow.get_last_active_trace_id()`` — set by MLflow's tracing
    fluent API when a ``@mlflow.trace``-decorated call returns. A ``None``
    here means either the harness ran without tracing configured or an
    MLflow backend failure — the trace-adapter path will short-circuit.
    """
    try:
        return mlflow.get_last_active_trace_id()
    except Exception:
        return None


def _fetch_trace(trace_id: str | None) -> Any | None:
    """Return the trace object for ``trace_id`` — required by the tool-call adapter."""
    if not trace_id:
        return None
    try:
        return mlflow.get_trace(trace_id)
    except Exception:
        return None


def _tool_calls_from_trace(trace_id: str | None) -> list[ToolCall]:
    """Materialise this run's tool calls from the freshly completed trace."""
    trace = _fetch_trace(trace_id)
    if trace is None:
        return []
    return trace_to_tool_calls(trace)


def _score_run(
    case: AgentEvalCase,
    *,
    run_index: int,
    result: RecommenderResult,
    tool_calls: list[ToolCall],
    latency_ms: float,
    trace_id: str | None,
) -> RunResult:
    """Populate one ``RunResult`` — tool selection + trajectory scores only.

    Goal completion is intentionally left ``None`` here — the batch
    scorer runner (Stage 2 wiring) computes it later against the same
    trace so the judge cache benefits from one shot per (case, answer)
    pair rather than one per harness re-invocation.
    """
    tool_selection = tool_selection_accuracy(tool_calls, case.expected_tools)
    precision, recall = trajectory_precision_recall(tool_calls, case.expected_tools)
    return RunResult(
        case_id=case.case_id,
        run_index=run_index,
        status=result.status,
        answer=result.answer,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
        trace_id=trace_id,
        error_type=result.error_type,
        tool_selection=tool_selection,
        traj_precision=precision,
        traj_recall=recall,
        goal_completion=None,
    )


def run_scenario(
    case: AgentEvalCase,
    *,
    temperature: float,
    run_count: int = 3,
    user_id: int = 1,
    model: str = CHEAP,
) -> ScenarioResult:
    """Run one scenario ``run_count`` times and return the aggregated result.

    Each run:

    1. Interpret ``case.input`` into a ``SearchIntent`` (fresh per run —
       the interpreter is deterministic-ish but not free, and the run
       needs an intent per call).
    2. Call ``run_once`` with ``request_origin="eval"``, a per-run
       ``eval_case_id`` tag, the ``temperature`` override, and
       ``record_runs=False`` so the eval harness does not pollute the
       Recommender's SQLite audit tables with 36 fake production rows.
    3. Grab the trace id via ``mlflow.get_last_active_trace_id`` and
       adapt its ``TOOL`` spans into ``ToolCall`` rows.
    4. Score tool-selection + trajectory precision/recall inline.
    """
    intent = _intent_for(case)
    runs: list[RunResult] = []
    for run_index in range(run_count):
        eval_case_id = f"{case.case_id}-run-{run_index}"
        run_context: dict[str, Any] = {
            "model": model,
            "text": case.input,
            "request_origin": "eval",
            "eval_case_id": eval_case_id,
            "temperature": temperature,
            "record_runs": False,
        }
        start = time.perf_counter()
        try:
            result = run_once(user_id, intent, **run_context)
        except Exception as exc:  # pragma: no cover - defensive: surface an error row
            latency_ms = (time.perf_counter() - start) * 1000.0
            runs.append(
                RunResult(
                    case_id=case.case_id,
                    run_index=run_index,
                    status="error",
                    answer=None,
                    tool_calls=[],
                    latency_ms=latency_ms,
                    trace_id=None,
                    error_type=f"harness_exception: {type(exc).__name__}: {exc}"[:200],
                )
            )
            continue
        latency_ms = (time.perf_counter() - start) * 1000.0
        trace_id = _fetch_trace_id()
        tool_calls = _tool_calls_from_trace(trace_id)
        runs.append(
            _score_run(
                case,
                run_index=run_index,
                result=result,
                tool_calls=tool_calls,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )
        )
    return ScenarioResult(case_id=case.case_id, runs=runs)


__all__ = ["run_scenario"]
