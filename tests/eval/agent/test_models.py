"""Unit tests for the HW4 agent-eval models.

Covers the `ToolCall.args_contains` subset rule and
`ScenarioResult.pass_at_3` / `pass_cubed` predicates — the two
reliability roll-ups the HW4 report cites.
"""

from __future__ import annotations

import pytest

from planazo.eval.agent.models import RunResult, ScenarioResult, ToolCall


# ---------------------------------------------------------------------------
# ToolCall.args_contains
# ---------------------------------------------------------------------------


def test_args_contains_exact_match() -> None:
    call = ToolCall(tool="search_events", arguments={"category": "tech", "city": "Barcelona"})
    assert call.args_contains({"category": "tech"}) is True


def test_args_contains_missing_key() -> None:
    call = ToolCall(tool="search_events", arguments={"category": "tech"})
    assert call.args_contains({"city": "Barcelona"}) is False


def test_args_contains_value_mismatch() -> None:
    call = ToolCall(tool="search_events", arguments={"category": "tech"})
    assert call.args_contains({"category": "music"}) is False


def test_args_contains_empty_expected_is_always_true() -> None:
    # An empty expected dict rewards any invocation of the tool.
    call = ToolCall(tool="search_events", arguments={"whatever": "ignored"})
    assert call.args_contains({}) is True


# ---------------------------------------------------------------------------
# ScenarioResult.pass_at_3 / pass_cubed
# ---------------------------------------------------------------------------


def _run(index: int, tool_selection: float | None) -> RunResult:
    return RunResult(
        case_id="test",
        run_index=index,
        status="ok",
        answer=None,
        tool_calls=[],
        latency_ms=1.0,
        tool_selection=tool_selection,
    )


def test_pass_at_3_true_when_any_run_passes() -> None:
    scenario = ScenarioResult(case_id="test", runs=[_run(0, 0.0), _run(1, 1.0), _run(2, 0.0)])
    assert scenario.pass_at_3() is True
    assert scenario.pass_cubed() is False


def test_pass_cubed_true_only_when_all_runs_pass() -> None:
    scenario = ScenarioResult(case_id="test", runs=[_run(0, 1.0), _run(1, 1.0), _run(2, 1.0)])
    assert scenario.pass_at_3() is True
    assert scenario.pass_cubed() is True


def test_pass_at_3_false_when_no_run_reaches_threshold() -> None:
    scenario = ScenarioResult(case_id="test", runs=[_run(0, 0.0), _run(1, 0.4), _run(2, 0.0)])
    assert scenario.pass_at_3() is False
    assert scenario.pass_cubed() is False


def test_pass_predicates_treat_none_selection_as_zero() -> None:
    scenario = ScenarioResult(case_id="test", runs=[_run(0, None), _run(1, None), _run(2, None)])
    assert scenario.pass_at_3() is False
    assert scenario.pass_cubed() is False


def test_pass_predicates_false_on_empty_runs() -> None:
    scenario = ScenarioResult(case_id="test", runs=[])
    assert scenario.pass_at_3() is False
    assert scenario.pass_cubed() is False


def test_custom_threshold_shifts_the_boundary() -> None:
    scenario = ScenarioResult(case_id="test", runs=[_run(0, 0.4), _run(1, 0.6), _run(2, 0.5)])
    assert scenario.pass_at_3(threshold=0.6) is True
    assert scenario.pass_cubed(threshold=0.6) is False
    assert scenario.pass_cubed(threshold=0.4) is True


# ---------------------------------------------------------------------------
# Pydantic guards
# ---------------------------------------------------------------------------


def test_toolcall_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        ToolCall(tool="", arguments={})
