import json
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import openai
import pytest

from agentlib.core import CHEAP, MODELS, STRONG, Result
from planazo.agents import cli, event_agent, loop
from planazo.agents.loop import LoopResult


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


@pytest.fixture(autouse=True)
def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A present key lets main() pass its pre-call check and reach the mocked
    # LLM; the missing-key test deletes it explicitly.
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")


def _tool_call_turns() -> tuple[Result, Result]:
    """A tool-calling turn (save_event_candidate) followed by an answering turn."""
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
        "arguments": json.dumps(tool_call["arguments"]),
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    return turn_1, turn_2


def _confirm_tool_call_turns() -> tuple[Result, Result]:
    """A tool-calling turn (confirm_and_create_calendar_event) followed by an answer."""
    tool_call = {
        "name": "confirm_and_create_calendar_event",
        "arguments": {"event_id": "evt-1", "notify_invitees": "none"},
        "call_id": "call_1",
    }
    output_item = {
        "type": "function_call",
        "name": "confirm_and_create_calendar_event",
        "arguments": json.dumps(tool_call["arguments"]),
        "call_id": "call_1",
    }
    turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    turn_2 = make_result(text="done", tool_calls=[], output_items=[])
    return turn_1, turn_2


def _redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # Run the REAL tools (only loop.call is mocked); just steer their JSON
    # into tmp files so the suite never writes into the repo's var/.
    candidates_path = tmp_path / "candidates.json"
    calendar_path = tmp_path / "calendar_events.json"
    monkeypatch.setattr("tools.tools.CANDIDATES_PATH", candidates_path)
    monkeypatch.setattr("tools.tools.CALENDAR_EVENTS_PATH", calendar_path)
    return calendar_path


def _seed_candidate(candidates_path: Path) -> None:
    """Pre-seed a saved candidate so confirm_and_create_calendar_event can find it."""
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "event_id": "evt-1",
                    "title": "AI Meetup",
                    "category": "tech",
                    "source": "meetup",
                    "start_time": "2026-08-01T19:00:00",
                    "location": "Barcelona",
                    "confidence": 0.9,
                }
            ]
        ),
        encoding="utf-8",
    )


def _fake_input(lines: list[str]) -> Callable[[str], str]:
    iterator: Iterator[str] = iter(lines)

    def _read(_prompt: str = "") -> str:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise EOFError from exc

    return _read


def test_one_shot_prints_the_tool_trace_the_answer_and_the_tally(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    turn_1, turn_2 = _tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    _redirect_stores(monkeypatch, tmp_path)

    code = cli.main(["--calendar", "save the AI Meetup event"])

    out = capsys.readouterr().out
    assert code == 0
    assert "step 1: save_event_candidate(" in out
    # The real tool's return shape, straight through the trace line.
    assert "'saved': {'id': 1, 'event_id': 'evt-1'" in out
    assert "'total_candidates': 1" in out
    assert "answer: done" in out
    assert "steps: 2" in out
    assert "stop reason: answered" in out


def test_one_shot_with_an_immediate_answer_prints_no_step_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        loop, "call", MagicMock(return_value=make_result(text="42", tool_calls=[], output_items=[]))
    )

    code = cli.main(["what is 6*7?"])

    out = capsys.readouterr().out
    assert code == 0
    assert "  step " not in out
    assert "answer: 42" in out
    assert "steps: 1" in out
    assert "stop reason: answered" in out


def test_strong_flag_forwards_the_strong_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock(return_value=make_result(tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    cli.main(["--strong", "hi"])

    forwarded = mock_call.call_args.kwargs["model"]
    assert forwarded == STRONG
    assert forwarded in MODELS.values()


def test_default_forwards_the_cheap_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock(return_value=make_result(tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    cli.main(["hi"])

    forwarded = mock_call.call_args.kwargs["model"]
    assert forwarded == CHEAP
    assert forwarded in MODELS.values()


def test_model_strong_by_name_forwards_the_strong_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock(return_value=make_result(tool_calls=[], output_items=[]))
    monkeypatch.setattr(loop, "call", mock_call)

    cli.main(["--model", "strong", "hi"])

    forwarded = mock_call.call_args.kwargs["model"]
    assert forwarded == STRONG
    assert forwarded in MODELS.values()


def test_conflicting_model_flags_are_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--strong", "--model", "cheap", "hi"])

    assert excinfo.value.code == 2
    mock_call.assert_not_called()


def test_repl_runs_one_prompt_then_exits_cleanly_on_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_call = MagicMock(
        return_value=make_result(text="hello there", tool_calls=[], output_items=[])
    )
    monkeypatch.setattr(loop, "call", mock_call)
    monkeypatch.setattr("builtins.input", _fake_input(["hi"]))

    code = cli.main([])

    out = capsys.readouterr().out
    assert code == 0
    assert mock_call.call_count == 1
    assert "answer: hello there" in out
    assert "bye" in out


def test_repl_exit_command_quits_without_calling_the_loop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)
    monkeypatch.setattr("builtins.input", _fake_input(["exit"]))

    code = cli.main([])

    out = capsys.readouterr().out
    assert code == 0
    mock_call.assert_not_called()
    assert "bye" in out


def test_missing_key_is_reported_before_the_loop_is_touched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)

    code = cli.main(["hi"])

    out = capsys.readouterr().out
    assert code == 1
    assert "OPENCODE_API_KEY" in out
    assert ".env" in out
    mock_call.assert_not_called()


def test_one_shot_provider_error_prints_a_one_liner_and_returns_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=openai.OpenAIError("invalid api key")))

    code = cli.main(["hi"])

    out = capsys.readouterr().out
    assert code != 0
    assert "invalid api key" in out
    assert "Traceback" not in out


def test_repl_provider_error_prints_and_continues_to_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=openai.OpenAIError("invalid api key")))
    monkeypatch.setattr("builtins.input", _fake_input(["hi"]))

    code = cli.main([])

    out = capsys.readouterr().out
    assert code == 0
    assert "invalid api key" in out
    assert "bye" in out


def test_unexpected_runtime_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=RuntimeError("unexpected boom")))

    with pytest.raises(RuntimeError, match="unexpected boom"):
        cli.main(["hi"])


def test_max_steps_run_reports_no_final_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
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
        "arguments": json.dumps(tool_call["arguments"]),
        "call_id": "call_1",
    }
    forever = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
    monkeypatch.setattr(loop, "call", MagicMock(return_value=forever))
    _redirect_stores(monkeypatch, tmp_path)

    code = cli.main(["--calendar", "--max-steps", "2", "loop forever"])

    out = capsys.readouterr().out
    assert code == 0
    assert "(no final answer — hit max steps)" in out
    assert "stop reason: max_steps" in out


def test_truncated_run_reports_a_partial_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cut_off = make_result(text="the partial ans", tool_calls=[], output_items=[], truncated=True)
    monkeypatch.setattr(loop, "call", MagicMock(return_value=cut_off))

    code = cli.main(["explain everything"])

    out = capsys.readouterr().out
    assert code == 0
    assert "partial answer (truncated by output cap): the partial ans" in out
    assert "stop reason: truncated" in out
    # The partial text must NOT render as a plain, trustworthy answer.
    assert "answer: the partial ans" not in out


def test_max_steps_below_one_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call = MagicMock()
    monkeypatch.setattr(loop, "call", mock_call)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--max-steps", "0", "hi"])

    assert excinfo.value.code == 2
    mock_call.assert_not_called()


def test_cli_prompts_for_approval_before_dispatching_irreversible_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    turn_1, turn_2 = _confirm_tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    calendar_path = _redirect_stores(monkeypatch, tmp_path)
    _seed_candidate(tmp_path / "candidates.json")
    fake_input = MagicMock(return_value="y")
    monkeypatch.setattr("builtins.input", fake_input)

    code = cli.main(["--calendar", "confirm the calendar event for evt-1"])

    out = capsys.readouterr().out
    assert code == 0
    assert fake_input.call_count >= 1
    prompt_call = fake_input.call_args.args[0]
    assert "approval required" in prompt_call
    assert "confirm_and_create_calendar_event" in prompt_call
    assert calendar_path.exists()
    persisted = json.loads(calendar_path.read_text())
    assert persisted[0]["event_id"] == "evt-1"
    assert "step 1: confirm_and_create_calendar_event(" in out
    assert "answer: done" in out


def test_cli_declines_when_user_answers_no(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    turn_1, turn_2 = _confirm_tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    calendar_path = _redirect_stores(monkeypatch, tmp_path)
    _seed_candidate(tmp_path / "candidates.json")
    monkeypatch.setattr("builtins.input", MagicMock(return_value="n"))

    code = cli.main(["--calendar", "confirm the calendar event for evt-1"])

    out = capsys.readouterr().out
    assert code == 0
    assert not calendar_path.exists()
    assert "'declined': True" in out
    assert "'reason': 'user_declined_approval'" in out
    assert "answer: done" in out


def test_cli_does_not_prompt_for_reversible_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    turn_1, turn_2 = _tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    _redirect_stores(monkeypatch, tmp_path)
    fake_input = MagicMock()
    monkeypatch.setattr("builtins.input", fake_input)

    code = cli.main(["--calendar", "save the AI Meetup event"])

    assert code == 0
    fake_input.assert_not_called()


def test_calendar_tools_are_unreachable_without_the_calendar_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run_loop = MagicMock(return_value=LoopResult(answer="ok", steps=1, stopped="answered"))
    monkeypatch.setattr(event_agent, "run_loop", mock_run_loop)

    code = cli.main(["hi"])

    assert code == 0
    assert set(mock_run_loop.call_args.kwargs["registry"]) == {"search_events"}


def test_cli_declines_on_eof_at_approval_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    turn_1, turn_2 = _confirm_tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    calendar_path = _redirect_stores(monkeypatch, tmp_path)
    _seed_candidate(tmp_path / "candidates.json")
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    code = cli.main(["--calendar", "confirm the calendar event for evt-1"])

    out = capsys.readouterr().out
    assert code == 0
    assert not calendar_path.exists()
    assert "'declined': True" in out
    assert "Traceback" not in out


def test_cli_declines_on_keyboard_interrupt_at_approval_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    turn_1, turn_2 = _confirm_tool_call_turns()
    monkeypatch.setattr(loop, "call", MagicMock(side_effect=[turn_1, turn_2]))
    calendar_path = _redirect_stores(monkeypatch, tmp_path)
    _seed_candidate(tmp_path / "candidates.json")
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=KeyboardInterrupt))

    code = cli.main(["--calendar", "confirm the calendar event for evt-1"])

    out = capsys.readouterr().out
    assert code == 0
    assert not calendar_path.exists()
    assert "'declined': True" in out
    assert "Traceback" not in out
