from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentlib.core import CHEAP, MODELS, STRONG, Result
from planazo.agents import event_agent, loop
from planazo.agents.loop import ApprovalGate, LoopResult


def make_result(**overrides: object) -> Result:
    defaults: dict[str, object] = {
        "text": "ok",
        "model": CHEAP,
        "status": "completed",
        "stop_reason": None,
        "truncated": False,
        "input_tokens": 13,
        "cached_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.000009,
        "reasoning_summary": None,
    }
    defaults.update(overrides)
    return Result(**defaults)  # type: ignore[arg-type]


def test_run_once_defaults_to_the_pinned_cheap_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="hi", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    event_agent.run_once("hi")

    forwarded_model = mock_call.call_args.kwargs["model"]
    assert forwarded_model == CHEAP
    assert forwarded_model in MODELS.values()
    assert forwarded_model != "gpt-4o"


def test_run_once_forwards_an_explicit_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="hi", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    event_agent.run_once("hi", model=STRONG)

    assert mock_call.call_args.kwargs["model"] == STRONG


def test_run_once_forwards_the_on_step_observer_to_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {
        "name": "save_event_candidate",
        "arguments": {
            "event_id": "evt-1",
            "title": "AI Meetup",
            "category": "tech",
            "source": "meetup",
            "start_time": "2026-08-01T19:00:00",
            "location": "Barcelona",
            "confidence": 0.9,
        },
        "call_id": "call_1",
    }
    output_item = {
        "type": "function_call",
        "name": "save_event_candidate",
        "arguments": (
            '{"event_id": "evt-1", "title": "AI Meetup", "category": "tech", '
            '"source": "meetup", "start_time": "2026-08-01T19:00:00", '
            '"location": "Barcelona", "confidence": 0.9}'
        ),
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    # Stub the tool so the forwarding test does not touch the on-disk store.
    stub_tool = MagicMock(return_value={"saved": "ok"})
    monkeypatch.setattr(event_agent, "TOOL_REGISTRY", {"save_event_candidate": stub_tool})

    records: list[loop.StepRecord] = []
    event_agent.run_once("hi", on_step=records.append)

    assert records == [
        loop.StepRecord(
            step=1,
            tool="save_event_candidate",
            arguments=tool_call["arguments"],
            result={"saved": "ok"},
        )
    ]


def test_run_once_forwards_the_gate_to_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run_loop = MagicMock(return_value=LoopResult(answer="ok", steps=1, stopped="answered"))
    monkeypatch.setattr(event_agent, "run_loop", mock_run_loop)
    gate = ApprovalGate(tool_names=frozenset(), approve=lambda *_a, **_kw: True)

    event_agent.run_once("hi", gate=gate)

    assert mock_run_loop.call_args.kwargs["gate"] is gate


def test_run_once_persists_a_validated_step_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool_call = {
        "name": "save_event_candidate",
        "arguments": {"event_id": "evt-1"},
        "call_id": "call_1",
    }
    turn_1 = make_result(
        text="",
        tool_calls=[tool_call],
        output_items=[{"type": "function_call", "call_id": "call_1"}],
    )
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    monkeypatch.setattr(
        event_agent,
        "TOOL_REGISTRY",
        {"save_event_candidate": MagicMock(return_value={"saved": True})},
    )

    event_agent.run_once("save an event", run_id="run-123", run_log_dir=tmp_path)

    trace = (tmp_path / "run-123.jsonl").read_text(encoding="utf-8")
    assert '"run_id":"run-123"' in trace
    assert '"model_tier":"cheap"' in trace
    assert '"tool_calls"' in trace
    assert '"phase":"completion"' in trace
    assert '"final_answer":"done"' in trace


def test_run_once_records_a_no_tool_answer_for_the_monitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        loop,
        "call",
        MagicMock(return_value=make_result(text="No events found", tool_calls=[], output_items=[])),
    )

    event_agent.run_once("find events", run_id="no-tools", run_log_dir=tmp_path)

    trace = (tmp_path / "no-tools.jsonl").read_text(encoding="utf-8")
    assert '"phase":"completion"' in trace
    assert '"final_answer":"No events found"' in trace


def test_run_once_forwards_max_output_tokens_to_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run_loop = MagicMock(return_value=LoopResult(answer="ok", steps=1, stopped="answered"))
    monkeypatch.setattr(event_agent, "run_loop", mock_run_loop)

    event_agent.run_once("hi", max_output_tokens=256)

    assert mock_run_loop.call_args.kwargs["max_output_tokens"] == 256
