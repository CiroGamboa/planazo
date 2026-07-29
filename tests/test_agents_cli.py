from datetime import UTC, datetime
from unittest.mock import MagicMock

import openai
import pytest

from agentlib.core import CHEAP, STRONG
from planazo.agents import cli
from planazo.agents.event_agent import RecommenderResult
from planazo.query.models import SearchIntent


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
    )


def _result(**overrides: object) -> RecommenderResult:
    values: dict[str, object] = {
        "status": "no_results",
        "answer": "Nothing matched.",
        "stopped": "answered",
        "steps": 1,
    }
    values.update(overrides)
    return RecommenderResult(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")


def test_cli_interprets_then_passes_typed_intent_and_user_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    intent = _intent()
    interpret = MagicMock(return_value=intent)
    run_once = MagicMock(return_value=_result())
    monkeypatch.setattr(cli, "interpret", interpret)
    monkeypatch.setattr(cli, "run_once", run_once)

    code = cli.main(["--user-id", "7", "find events"])

    assert code == 0
    interpret.assert_called_once_with("find events")
    assert run_once.call_args.args == (7, intent)
    assert run_once.call_args.kwargs["model"] == CHEAP
    assert "status: no_results" in capsys.readouterr().out


def test_cli_forwards_model_and_calendar_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "interpret", MagicMock(return_value=_intent()))
    run_once = MagicMock(return_value=_result())
    monkeypatch.setattr(cli, "run_once", run_once)

    assert (
        cli.main(["--user-id", "7", "--strong", "--calendar", "--max-steps", "3", "find events"])
        == 0
    )

    assert run_once.call_args.kwargs["model"] == STRONG
    assert run_once.call_args.kwargs["calendar_enabled"] is True
    assert run_once.call_args.kwargs["max_steps"] == 3


@pytest.mark.parametrize(
    ("answer", "expected"), [("y", True), ("yes", True), ("n", False), ("", False)]
)
def test_calendar_approval_prompt_is_explicit_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert (
        cli._terminal_approve("confirm_and_create_calendar_event", {"event_id": "evt-1"})
        is expected
    )


def test_calendar_approval_prompt_declines_on_missing_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    assert cli._terminal_approve("confirm_and_create_calendar_event", {}) is False


def test_cli_provider_error_is_a_single_nonzero_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "interpret", MagicMock(return_value=_intent()))
    monkeypatch.setattr(cli, "run_once", MagicMock(side_effect=openai.OpenAIError("provider down")))

    assert cli.main(["--user-id", "7", "find events"]) == 1
    assert capsys.readouterr().out.strip() == "provider down"


def test_conflicting_model_flags_are_a_usage_error() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--user-id", "7", "--strong", "--model", "cheap", "find events"])

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("result", "needle", "exit_code"),
    [
        (
            _result(
                status="error",
                answer="safe",
                stopped="not_started",
                steps=0,
                error_type="invalid_preference_data",
            ),
            "configuration/data-safe failure",
            1,
        ),
        (
            _result(
                status="error",
                answer="safe",
                stopped="not_started",
                steps=0,
                error_type="missing_search_origin",
            ),
            "trusted search origin",
            1,
        ),
        (
            _result(
                status="error",
                answer="invalid payload",
                error_type="invalid_search_output",
            ),
            "search error (invalid_search_output)",
            1,
        ),
        (
            _result(status="incomplete", answer="partial", stopped="truncated"),
            "incomplete (truncated)",
            0,
        ),
    ],
)
def test_cli_renders_typed_results_and_uses_error_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: RecommenderResult,
    needle: str,
    exit_code: int,
) -> None:
    monkeypatch.setattr(cli, "interpret", MagicMock(return_value=_intent()))
    monkeypatch.setattr(cli, "run_once", MagicMock(return_value=result))

    assert cli.main(["--user-id", "7", "find events"]) == exit_code
    assert needle in capsys.readouterr().out


def test_cli_renders_clarification_without_error_exit() -> None:
    rendered = cli._render_result(
        _result(
            status="needs_clarification",
            answer=None,
            clarification={"question": "Which area?"},
        )
    )

    assert "clarification needed: Which area?" in rendered


def test_invalid_user_id_and_max_steps_are_usage_errors() -> None:
    with pytest.raises(SystemExit) as user_id:
        cli.main(["--user-id", "0", "find events"])
    with pytest.raises(SystemExit) as max_steps:
        cli.main(["--max-steps", "0", "find events"])

    assert user_id.value.code == max_steps.value.code == 2


def test_missing_key_returns_before_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    interpret = MagicMock()
    monkeypatch.setattr(cli, "interpret", interpret)

    assert cli.main(["--user-id", "7", "find events"]) == 1
    interpret.assert_not_called()


def test_repl_passes_each_nonempty_prompt_to_the_typed_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = iter(["find events", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(cli, "interpret", MagicMock(return_value=_intent()))
    run_once = MagicMock(return_value=_result())
    monkeypatch.setattr(cli, "run_once", run_once)

    assert cli.main(["--user-id", "7"]) == 0

    assert run_once.call_args.args[0] == 7
    assert "bye" in capsys.readouterr().out


def test_cli_requires_an_explicit_developer_identity() -> None:
    with pytest.raises(SystemExit) as missing_user:
        cli.main(["find events"])

    assert missing_user.value.code == 2
