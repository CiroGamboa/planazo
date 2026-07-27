import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentlib import core


class FakeItem:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def make_usage(input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int):
    return SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        total_tokens=input_tokens + output_tokens,
    )


def make_response(
    *,
    output_text: str = "ok",
    status: str = "completed",
    output: list | None = None,
    usage=None,
    incomplete_reason: str | None = None,
):
    return SimpleNamespace(
        output_text=output_text,
        status=status,
        output=output or [],
        usage=usage if usage is not None else make_usage(10, 0, 5, 0),
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
    )


def test_cost_matches_hand_calculation() -> None:
    usage = make_usage(input_tokens=5204, cached_tokens=4864, output_tokens=20, reasoning_tokens=0)
    in_rate, cached_rate, out_rate = core.PRICES[core.CHEAP]
    by_hand = (
        (usage.input_tokens - usage.input_tokens_details.cached_tokens) * in_rate
        + usage.input_tokens_details.cached_tokens * cached_rate
        + usage.output_tokens * out_rate
    ) / 1_000_000
    assert core.cost(usage, core.CHEAP) == pytest.approx(by_hand)


def test_call_returns_completed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = make_usage(13, 0, 5, 0)
    monkeypatch.setattr(
        core.client.responses,
        "create",
        MagicMock(
            return_value=make_response(
                output_text="ok",
                status="completed",
                usage=usage,
            )
        ),
    )

    r = core.call("Reply with exactly one word: ok", model=core.CHEAP, max_output_tokens=16)

    assert r.text == "ok"
    assert r.status == "completed"
    assert r.stop_reason is None
    assert r.truncated is False
    assert r.input_tokens == 13
    assert r.output_tokens == 5
    assert r.cost_usd == pytest.approx(core.cost(usage, core.CHEAP))


def test_call_flags_truncation_on_max_output_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core.client.responses,
        "create",
        MagicMock(
            return_value=make_response(
                output_text='{"partial":',
                status="incomplete",
                incomplete_reason="max_output_tokens",
            )
        ),
    )

    r = core.call("prompt", model=core.CHEAP, max_output_tokens=40)

    assert r.status == "incomplete"
    assert r.stop_reason == "max_output_tokens"
    assert r.truncated is True


def test_call_parses_tool_calls_and_keeps_output_items(monkeypatch: pytest.MonkeyPatch) -> None:
    function_call_item = {
        "type": "function_call",
        "name": "add",
        "arguments": '{"a": 17, "b": 23}',
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        core.client.responses,
        "create",
        MagicMock(
            return_value=make_response(
                output_text="",
                output=[FakeItem(function_call_item)],
            )
        ),
    )

    r = core.call(
        messages=[{"role": "user", "content": "add 17 and 23"}], model=core.CHEAP, tools=[]
    )

    assert r.tool_calls == [{"name": "add", "arguments": {"a": 17, "b": 23}, "call_id": "call_1"}]
    assert r.output_items == [function_call_item]


def test_call_joins_reasoning_summary_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    reasoning_item = {
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": "first part"},
            {"type": "summary_text", "text": "second part"},
        ],
    }
    monkeypatch.setattr(
        core.client.responses,
        "create",
        MagicMock(
            return_value=make_response(
                output=[FakeItem(reasoning_item)],
            )
        ),
    )

    r = core.call("puzzle", model=core.STRONG, reasoning_effort="high", reasoning_summary="auto")

    assert r.reasoning_summary == "first part\n\nsecond part"


def test_call_omits_reasoning_param_when_effort_is_none_or_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_create = MagicMock(return_value=make_response())
    monkeypatch.setattr(core.client.responses, "create", mock_create)

    core.call("hi", model=core.CHEAP, reasoning_effort="none")
    assert "reasoning" not in mock_create.call_args.kwargs

    core.call("hi", model=core.CHEAP)
    assert "reasoning" not in mock_create.call_args.kwargs


def test_call_sends_reasoning_effort_and_omits_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_create = MagicMock(return_value=make_response())
    monkeypatch.setattr(core.client.responses, "create", mock_create)

    core.call("puzzle", model=core.STRONG, reasoning_effort="high")

    assert mock_create.call_args.kwargs["reasoning"] == {"effort": "high"}
    assert "temperature" not in mock_create.call_args.kwargs


def test_call_sends_explicit_temperature_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_create = MagicMock(return_value=make_response())
    monkeypatch.setattr(core.client.responses, "create", mock_create)

    core.call("hi", model=core.CHEAP, temperature=0)

    assert mock_create.call_args.kwargs["temperature"] == 0


def test_call_requires_prompt_or_messages() -> None:
    with pytest.raises(ValueError, match="requires either"):
        core.call(model=core.CHEAP)


def test_call_rejects_both_prompt_and_messages() -> None:
    with pytest.raises(ValueError, match="not both"):
        core.call("hi", messages=[{"role": "user", "content": "hi"}], model=core.CHEAP)


def test_show_prints_one_line_summary(capsys: pytest.CaptureFixture[str]) -> None:
    result = core.Result(
        text="ok",
        model=core.CHEAP,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=13,
        cached_tokens=0,
        output_tokens=5,
        reasoning_tokens=0,
        cost_usd=0.000009,
        reasoning_summary=None,
    )
    core.show(result, "A0")
    out = capsys.readouterr().out
    assert out == (
        f"[A0] {core.CHEAP} | in=13 (cached 0) out=5 (reasoning 0) | "
        "$0.000009 (0.0009¢) | status=completed\n"
    )


def test_think_aloud_retries_until_summary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = core.Result(
        text="",
        model=core.STRONG,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=1,
        cost_usd=0.0,
        reasoning_summary=None,
    )
    filled = core.Result(
        text="answer",
        model=core.STRONG,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=1,
        cost_usd=0.0,
        reasoning_summary="the reasoning",
    )
    mock_call = MagicMock(side_effect=[empty, filled])
    monkeypatch.setattr(core, "call", mock_call)

    r = core.think_aloud("puzzle", max_retries=5)

    assert r is filled
    assert mock_call.call_count == 2


def test_think_aloud_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = core.Result(
        text="",
        model=core.STRONG,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=1,
        cost_usd=0.0,
        reasoning_summary=None,
    )
    mock_call = MagicMock(return_value=empty)
    monkeypatch.setattr(core, "call", mock_call)

    r = core.think_aloud("puzzle", max_retries=3)

    assert r is empty
    assert mock_call.call_count == 3


def test_importing_core_does_not_require_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delitem(sys.modules, "agentlib.core", raising=False)
    try:
        importlib.import_module("agentlib.core")  # must not raise
    finally:
        sys.modules.pop("agentlib.core", None)
        monkeypatch.setitem(os.environ, "OPENCODE_API_KEY", "test-key-not-real")
        importlib.import_module("agentlib.core")


def test_missing_api_key_raises_on_first_client_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_client", None)  # force re-resolution
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        core.call("hi", model=core.CHEAP)
