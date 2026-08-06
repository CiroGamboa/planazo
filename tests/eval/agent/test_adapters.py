"""Unit tests for the trace→scorer-input adapters (HW4 Part 2 wiring).

The adapters read an MLflow `Trace` — the tests build fake spans that
match the shape autolog and `@mlflow.trace` produce (span type,
`mlflow.spanInputs`, `mlflow.spanOutputs`, `start_time_ns`), so a full
LLM/agent run is never required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from planazo.eval.agent.adapters import (
    trace_to_generation_inputs,
    trace_to_retrieval_inputs,
    trace_to_tool_calls,
)


@dataclass
class _FakeSpan:
    """Minimal span shape the adapters read."""

    name: str
    span_type: str
    start_time_ns: int
    attributes: dict[str, Any]


@dataclass
class _FakeTraceData:
    spans: list[_FakeSpan]


@dataclass
class _FakeTrace:
    data: _FakeTraceData


def _agent_span(inputs: dict[str, Any], outputs: dict[str, Any] | None = None) -> _FakeSpan:
    attrs: dict[str, Any] = {"mlflow.spanInputs": inputs}
    if outputs is not None:
        attrs["mlflow.spanOutputs"] = outputs
    return _FakeSpan(
        name="recommender.run_once",
        span_type="AGENT",
        start_time_ns=1_000,
        attributes=attrs,
    )


def _tool_span(name: str, arguments: dict[str, Any], start_ns: int) -> _FakeSpan:
    return _FakeSpan(
        name=name,
        span_type="TOOL",
        start_time_ns=start_ns,
        attributes={"mlflow.spanInputs": arguments},
    )


def _retriever_span(events: list[dict[str, Any]], start_ns: int = 3_000) -> _FakeSpan:
    return _FakeSpan(
        name="search_events_rag",
        span_type="RETRIEVER",
        start_time_ns=start_ns,
        attributes={"mlflow.spanOutputs": events},
    )


# ---------------------------------------------------------------------------
# trace_to_tool_calls
# ---------------------------------------------------------------------------


def test_trace_to_tool_calls_returns_ordered_tool_calls() -> None:
    trace = _FakeTrace(
        _FakeTraceData(
            [
                _tool_span("search_events", {"category": "tech"}, start_ns=2_000),
                _tool_span("retrieve_memory", {"query": "tech"}, start_ns=1_500),
                _agent_span({}, {}),
            ]
        )
    )
    calls = trace_to_tool_calls(trace)
    assert [c.tool for c in calls] == ["retrieve_memory", "search_events"]
    assert calls[0].arguments == {"query": "tech"}
    assert calls[1].arguments == {"category": "tech"}


def test_trace_to_tool_calls_empty_when_no_tool_spans() -> None:
    trace = _FakeTrace(_FakeTraceData([_agent_span({}, {})]))
    assert trace_to_tool_calls(trace) == []


def test_trace_to_tool_calls_missing_arguments_defaults_to_empty_dict() -> None:
    # Autolog occasionally strips `mlflow.spanInputs`; the adapter must
    # still yield a ToolCall so the trajectory metric can score it.
    span = _FakeSpan(
        name="search_events",
        span_type="TOOL",
        start_time_ns=1_000,
        attributes={},
    )
    calls = trace_to_tool_calls(_FakeTrace(_FakeTraceData([span])))
    assert len(calls) == 1
    assert calls[0].tool == "search_events"
    assert calls[0].arguments == {}


# ---------------------------------------------------------------------------
# trace_to_retrieval_inputs
# ---------------------------------------------------------------------------


def test_trace_to_retrieval_inputs_projects_event_ids() -> None:
    events = [{"id": 3, "title": "e3"}, {"id": 5, "title": "e5"}]
    trace = _FakeTrace(_FakeTraceData([_retriever_span(events)]))
    assert trace_to_retrieval_inputs(trace) == ["3", "5"]


def test_trace_to_retrieval_inputs_none_when_no_retriever_span() -> None:
    trace = _FakeTrace(_FakeTraceData([_agent_span({}, {})]))
    assert trace_to_retrieval_inputs(trace) is None


def test_trace_to_retrieval_inputs_none_when_output_not_list() -> None:
    span = _FakeSpan(
        name="search_events_rag",
        span_type="RETRIEVER",
        start_time_ns=1_000,
        attributes={"mlflow.spanOutputs": {"error": "oops"}},
    )
    assert trace_to_retrieval_inputs(_FakeTrace(_FakeTraceData([span]))) is None


def test_trace_to_retrieval_inputs_skips_rows_without_id() -> None:
    events = [{"id": 3}, {"title": "no id"}, {"id": None}, {"id": 7}]
    trace = _FakeTrace(_FakeTraceData([_retriever_span(events)]))
    assert trace_to_retrieval_inputs(trace) == ["3", "7"]


# ---------------------------------------------------------------------------
# trace_to_generation_inputs
# ---------------------------------------------------------------------------


def test_trace_to_generation_inputs_none_when_no_agent_span() -> None:
    trace = _FakeTrace(_FakeTraceData([_retriever_span([{"id": 1}])]))
    assert trace_to_generation_inputs(trace) is None


def test_trace_to_generation_inputs_none_when_no_retriever_span() -> None:
    trace = _FakeTrace(
        _FakeTraceData([_agent_span({"text": "q"}, {"answer": "a"})])
    )
    assert trace_to_generation_inputs(trace) is None
