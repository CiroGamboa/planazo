import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentlib.core import CHEAP, Result
from planazo.agents import loop


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


# Test-only stub tool — not exported, not real product code. Only exists to
# exercise run_loop's dispatch mechanics against a hand-written schema shaped
# exactly like agentlib.core.call's flat function-schema contract.
def _echo(text: str) -> str:
    return text.upper()


_ECHO_SCHEMA: dict[str, object] = {
    "type": "function",
    "name": "echo",
    "description": "Echo the input text back, upper-cased.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}


def test_run_loop_returns_immediately_when_model_answers_without_a_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="42", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    result = loop.run_loop("what is 6*7?", [], {}, model=CHEAP)

    assert result == loop.LoopResult(answer="42", steps=1, stopped="answered")
    assert mock_call.call_count == 1


def test_run_loop_reports_truncated_when_the_final_answer_is_cut_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(
        return_value=make_result(
            text="the partial ans", tool_calls=[], output_items=[], truncated=True
        )
    )
    monkeypatch.setattr(loop, "call", mock_call)

    result = loop.run_loop("explain everything", [], {}, model=CHEAP)

    assert result == loop.LoopResult(answer="the partial ans", steps=1, stopped="truncated")
    assert mock_call.call_count == 1


def test_run_loop_dispatches_a_tool_call_and_feeds_the_result_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)
    mock_echo = MagicMock(side_effect=_echo)

    result = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": mock_echo}, model=CHEAP)

    mock_echo.assert_called_once_with(text="hi")
    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")
    assert mock_call.call_count == 2

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    assert second_call_messages == [
        {"role": "user", "content": "echo hi"},
        output_item,
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps({"result": _echo("hi")}),
        },
    ]


def test_run_loop_dispatches_multiple_tool_calls_in_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call_1 = {"name": "echo", "arguments": {"text": "a"}, "call_id": "call_1"}
    tool_call_2 = {"name": "echo", "arguments": {"text": "b"}, "call_id": "call_2"}
    output_items = [
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "a"}',
            "call_id": "call_1",
        },
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "b"}',
            "call_id": "call_2",
        },
    ]
    turn_1 = make_result(text="", tool_calls=[tool_call_1, tool_call_2], output_items=output_items)
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)
    mock_echo = MagicMock(side_effect=_echo)

    result = loop.run_loop("echo a and b", [_ECHO_SCHEMA], {"echo": mock_echo}, model=CHEAP)

    assert mock_echo.call_count == 2
    mock_echo.assert_any_call(text="a")
    mock_echo.assert_any_call(text="b")
    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    function_call_outputs = [
        m for m in second_call_messages if m.get("type") == "function_call_output"
    ]
    assert function_call_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps({"result": _echo("a")}),
        },
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": json.dumps({"result": _echo("b")}),
        },
    ]


def test_run_loop_records_a_step_for_each_tool_call_with_the_turn_step_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    records: list[loop.StepRecord] = []
    result = loop.run_loop(
        "echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, on_step=records.append
    )

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")
    assert records == [
        loop.StepRecord(step=1, tool="echo", arguments={"text": "hi"}, result=_echo("hi"))
    ]


def test_run_loop_records_two_steps_sharing_one_step_number_for_a_two_call_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call_1 = {"name": "echo", "arguments": {"text": "a"}, "call_id": "call_1"}
    tool_call_2 = {"name": "echo", "arguments": {"text": "b"}, "call_id": "call_2"}
    output_items = [
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "a"}',
            "call_id": "call_1",
        },
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "b"}',
            "call_id": "call_2",
        },
    ]
    turn_1 = make_result(text="", tool_calls=[tool_call_1, tool_call_2], output_items=output_items)
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    records: list[loop.StepRecord] = []
    loop.run_loop(
        "echo a and b", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, on_step=records.append
    )

    assert records == [
        loop.StepRecord(step=1, tool="echo", arguments={"text": "a"}, result=_echo("a")),
        loop.StepRecord(step=1, tool="echo", arguments={"text": "b"}, result=_echo("b")),
    ]


def test_run_loop_with_no_observer_is_a_no_op_returning_the_same_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])

    def make_call() -> MagicMock:
        return MagicMock(side_effect=[turn_1, turn_2])

    monkeypatch.setattr(loop, "call", make_call())
    with_default = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP)

    monkeypatch.setattr(loop, "call", make_call())
    with_none = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, on_step=None)

    assert with_default == with_none == loop.LoopResult(answer="done", steps=2, stopped="answered")


def test_run_loop_stops_at_max_steps_when_the_model_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    mock_call = MagicMock(
        return_value=make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    )
    monkeypatch.setattr(loop, "call", mock_call)

    result = loop.run_loop(
        "echo forever", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, max_steps=3
    )

    assert result == loop.LoopResult(answer=None, steps=3, stopped="max_steps")
    assert mock_call.call_count == 3


def test_run_loop_rejects_a_non_positive_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)

    with pytest.raises(ValueError):
        loop.run_loop("hi", [], {}, model=CHEAP, max_steps=0)

    mock_call.assert_not_called()


def test_run_loop_bypasses_gate_when_tool_not_in_gated_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    approve = MagicMock(return_value=True)
    gate = loop.ApprovalGate(tool_names=frozenset({"never_matches"}), approve=approve)

    result = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, gate=gate)

    approve.assert_not_called()
    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")


def test_run_loop_calls_approve_before_dispatching_a_gated_tool_and_runs_on_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    order: list[str] = []
    approve = MagicMock(side_effect=lambda *_a, **_kw: order.append("approve") or True)
    mock_echo = MagicMock(side_effect=lambda **kw: order.append("echo") or _echo(**kw))
    gate = loop.ApprovalGate(tool_names=frozenset({"echo"}), approve=approve)

    result = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": mock_echo}, model=CHEAP, gate=gate)

    approve.assert_called_once_with("echo", {"text": "hi"})
    mock_echo.assert_called_once_with(text="hi")
    assert order == ["approve", "echo"]
    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    function_call_outputs = [
        m for m in second_call_messages if m.get("type") == "function_call_output"
    ]
    assert function_call_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps({"result": _echo("hi")}),
        }
    ]


def test_run_loop_skips_dispatch_and_emits_declined_result_when_approve_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="ok, understood", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)
    approve = MagicMock(return_value=False)
    mock_echo = MagicMock(side_effect=_echo)
    gate = loop.ApprovalGate(tool_names=frozenset({"echo"}), approve=approve)

    result = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": mock_echo}, model=CHEAP, gate=gate)

    approve.assert_called_once_with("echo", {"text": "hi"})
    mock_echo.assert_not_called()
    assert result == loop.LoopResult(answer="ok, understood", steps=2, stopped="answered")

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    function_call_outputs = [
        m for m in second_call_messages if m.get("type") == "function_call_output"
    ]
    assert function_call_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps({"result": loop.DECLINED_RESULT}),
        }
    ]


def test_run_loop_records_step_with_declined_result_for_declined_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="ok", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    gate = loop.ApprovalGate(tool_names=frozenset({"echo"}), approve=lambda *_a, **_kw: False)

    records: list[loop.StepRecord] = []
    loop.run_loop(
        "echo hi",
        [_ECHO_SCHEMA],
        {"echo": _echo},
        model=CHEAP,
        on_step=records.append,
        gate=gate,
    )

    assert records == [
        loop.StepRecord(step=1, tool="echo", arguments={"text": "hi"}, result=loop.DECLINED_RESULT)
    ]


def test_run_loop_with_no_gate_is_identical_to_current_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])

    def make_call() -> MagicMock:
        return MagicMock(side_effect=[turn_1, turn_2])

    monkeypatch.setattr(loop, "call", make_call())
    baseline = loop.run_loop("echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP)

    monkeypatch.setattr(loop, "call", make_call())
    with_none_gate = loop.run_loop(
        "echo hi", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, gate=None
    )

    assert baseline == with_none_gate == loop.LoopResult(answer="done", steps=2, stopped="answered")


def test_run_loop_forwards_max_output_tokens_to_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="ok", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    loop.run_loop("hi", [], {}, model=CHEAP, max_output_tokens=256)

    assert mock_call.call_args.kwargs["max_output_tokens"] == 256

    mock_call.reset_mock()
    monkeypatch.setattr(loop, "call", mock_call)
    loop.run_loop("hi", [], {}, model=CHEAP)
    assert "max_output_tokens" not in mock_call.call_args.kwargs


def test_run_loop_rejects_non_positive_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)

    with pytest.raises(ValueError):
        loop.run_loop("hi", [], {}, model=CHEAP, max_output_tokens=0)

    mock_call.assert_not_called()


def test_run_loop_catches_a_raising_tool_and_feeds_back_a_failure_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    def _boom(text: str) -> str:
        raise RuntimeError("kaboom")

    records: list[loop.StepRecord] = []
    result = loop.run_loop(
        "echo hi", [_ECHO_SCHEMA], {"echo": _boom}, model=CHEAP, on_step=records.append
    )

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")
    assert len(records) == 1
    assert records[0].result == {"tool_failed": True, "error": "RuntimeError: kaboom"}

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    function_call_outputs = [
        m for m in second_call_messages if m.get("type") == "function_call_output"
    ]
    assert function_call_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps(
                {"result": {"tool_failed": True, "error": "RuntimeError: kaboom"}}
            ),
        }
    ]


def test_run_loop_catches_an_unserializable_result_as_a_failure_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "echo",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))

    def _returns_a_set(text: str) -> set[str]:
        return {text}

    records: list[loop.StepRecord] = []
    result = loop.run_loop(
        "echo hi", [_ECHO_SCHEMA], {"echo": _returns_a_set}, model=CHEAP, on_step=records.append
    )

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")
    assert len(records) == 1
    marker = records[0].result
    assert marker["tool_failed"] is True
    assert "serializable" in marker["error"]


def test_run_loop_catches_an_unregistered_tool_name_as_a_failure_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = {"name": "mystery", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "mystery",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))

    records: list[loop.StepRecord] = []
    result = loop.run_loop(
        "use mystery", [_ECHO_SCHEMA], {"echo": _echo}, model=CHEAP, on_step=records.append
    )

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")
    assert records[0].result == {"tool_failed": True, "error": "unknown tool: mystery"}


def test_run_loop_failure_markers_are_legible_not_cryptic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raise_call = {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    unknown_call = {"name": "mystery", "arguments": {"text": "hi"}, "call_id": "call_2"}
    output_items = [
        {"type": "function_call", "name": "echo", "arguments": "{}", "call_id": "call_1"},
        {"type": "function_call", "name": "mystery", "arguments": "{}", "call_id": "call_2"},
    ]
    turn_1 = make_result(text="", tool_calls=[raise_call, unknown_call], output_items=output_items)
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))

    def _boom(text: str) -> str:
        raise ValueError("bad input")

    records: list[loop.StepRecord] = []
    loop.run_loop(
        "echo then mystery",
        [_ECHO_SCHEMA],
        {"echo": _boom},
        model=CHEAP,
        on_step=records.append,
    )

    raise_error = records[0].result["error"]
    unknown_error = records[1].result["error"]
    assert raise_error == "ValueError: bad input"
    assert unknown_error == "unknown tool: mystery"
    assert "KeyError" not in unknown_error
    assert all(rec.result["error"] for rec in records)


def test_run_loop_continues_the_turn_after_a_failed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_call = {"name": "echo", "arguments": {"text": "boom"}, "call_id": "call_1"}
    ok_call = {"name": "echo", "arguments": {"text": "ok"}, "call_id": "call_2"}
    output_items = [
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "boom"}',
            "call_id": "call_1",
        },
        {
            "type": "function_call",
            "name": "echo",
            "arguments": '{"text": "ok"}',
            "call_id": "call_2",
        },
    ]
    turn_1 = make_result(text="", tool_calls=[failing_call, ok_call], output_items=output_items)
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    def _echo_or_raise(text: str) -> str:
        if text == "boom":
            raise RuntimeError("kaboom")
        return _echo(text)

    result = loop.run_loop(
        "echo boom then ok", [_ECHO_SCHEMA], {"echo": _echo_or_raise}, model=CHEAP
    )

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")

    second_call_messages = mock_call.call_args_list[1].kwargs["messages"]
    function_call_outputs = [
        m for m in second_call_messages if m.get("type") == "function_call_output"
    ]
    assert function_call_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps(
                {"result": {"tool_failed": True, "error": "RuntimeError: kaboom"}}
            ),
        },
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": json.dumps({"result": _echo("ok")}),
        },
    ]


def test_run_loop_is_generic_over_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mechanically enforce genericity: loop.py's source must not reference
    # this test's tool name (or any other tool-domain literal) so that
    # swapping in a different tools/registry pair is a caller-side change only.
    source = Path(loop.__file__).read_text()
    assert "echo" not in source

    def _shout(text: str) -> str:
        return text.upper()

    shout_schema: dict[str, object] = {
        "type": "function",
        "name": "shout",
        "description": "Shout the input text back.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    }
    tool_call = {"name": "shout", "arguments": {"text": "hi"}, "call_id": "call_1"}
    output_item = {
        "type": "function_call",
        "name": "shout",
        "arguments": '{"text": "hi"}',
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    mock_call = MagicMock(side_effect=[turn_1, turn_2])
    monkeypatch.setattr(loop, "call", mock_call)

    result = loop.run_loop("shout hi", [shout_schema], {"shout": _shout}, model=CHEAP)

    assert result == loop.LoopResult(answer="done", steps=2, stopped="answered")


def test_run_loop_opens_with_a_system_message_when_one_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="42", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    loop.run_loop("what is 6*7?", [], {}, model=CHEAP, system="RULES")

    assert mock_call.call_args.kwargs["messages"] == [
        {"role": "system", "content": "RULES"},
        {"role": "user", "content": "what is 6*7?"},
    ]


def test_run_loop_without_a_system_prompt_opens_with_the_user_message_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call = MagicMock(return_value=make_result(text="42", tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    loop.run_loop("what is 6*7?", [], {}, model=CHEAP)

    assert mock_call.call_args.kwargs["messages"] == [{"role": "user", "content": "what is 6*7?"}]
