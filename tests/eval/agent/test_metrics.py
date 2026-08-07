"""Unit tests for the HW4 agent-eval trajectory metrics.

Covers the three Part 1 metrics — `tool_selection_accuracy`,
`trajectory_precision_recall`, `goal_completion_score` — with the same
fixture patterns HW3 used (`MockJudge` from
`tests/eval/test_generation_scorers.py`) so a caller can eyeball the
scoring rules against concrete cases without spinning up the LLM.
"""

from __future__ import annotations

from planazo.eval.agent.metrics import (
    goal_completion_score,
    tool_selection_accuracy,
    trajectory_precision_recall,
)
from planazo.eval.agent.models import ExpectedToolCall, ToolCall
from planazo.eval.judge import JudgeCacheKey, JudgeResponse, LLMJudge


class _MockJudge(LLMJudge):
    """Scripted `LLMJudge` — pops the next `JudgeResponse` per call.

    Matches the `judge(prompt, *, cache_key)` signature of the concrete
    `OpenCodeJudge` in `planazo.eval.judge`.
    """

    def __init__(self, responses: list[JudgeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, JudgeCacheKey]] = []

    def judge(self, prompt: str, *, cache_key: JudgeCacheKey) -> JudgeResponse:
        self.calls.append((prompt, cache_key))
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# tool_selection_accuracy
# ---------------------------------------------------------------------------


def test_tool_selection_full_match() -> None:
    actual = [ToolCall(tool="search_events", arguments={"category": "tech"})]
    expected = [ExpectedToolCall(tool="search_events", args_contains={"category": "tech"})]
    assert tool_selection_accuracy(actual, expected) == 1.0


def test_tool_selection_partial_match() -> None:
    # Two expected, one actually called → 0.5.
    actual = [ToolCall(tool="search_events", arguments={})]
    expected = [
        ExpectedToolCall(tool="search_events"),
        ExpectedToolCall(tool="save_memory"),
    ]
    assert tool_selection_accuracy(actual, expected) == 0.5


def test_tool_selection_no_match() -> None:
    actual = [ToolCall(tool="retrieve_memory", arguments={})]
    expected = [ExpectedToolCall(tool="search_events")]
    assert tool_selection_accuracy(actual, expected) == 0.0


def test_tool_selection_empty_expected_and_actual_is_full_score() -> None:
    # Preflight abort path: no tool expected, none called → 1.0 (accept).
    assert tool_selection_accuracy([], []) == 1.0


def test_tool_selection_ignores_extra_tools_beyond_expected() -> None:
    # `retrieve_memory` extra is not penalised by tool_selection (that is
    # what `trajectory_precision_recall` catches).
    actual = [
        ToolCall(tool="retrieve_memory", arguments={}),
        ToolCall(tool="search_events", arguments={}),
    ]
    expected = [ExpectedToolCall(tool="search_events")]
    assert tool_selection_accuracy(actual, expected) == 1.0


def test_tool_selection_respects_args_contains() -> None:
    actual = [ToolCall(tool="search_events", arguments={"category": "food"})]
    expected = [ExpectedToolCall(tool="search_events", args_contains={"category": "tech"})]
    assert tool_selection_accuracy(actual, expected) == 0.0


# ---------------------------------------------------------------------------
# trajectory_precision_recall
# ---------------------------------------------------------------------------


def test_trajectory_precision_and_recall_full_match() -> None:
    actual = [ToolCall(tool="search_events", arguments={})]
    expected = [ExpectedToolCall(tool="search_events")]
    precision, recall = trajectory_precision_recall(actual, expected)
    assert precision == 1.0
    assert recall == 1.0


def test_trajectory_precision_drops_when_extra_tools_called() -> None:
    # Two calls, one matches expected → precision 0.5, recall 1.0.
    actual = [
        ToolCall(tool="retrieve_memory", arguments={}),
        ToolCall(tool="search_events", arguments={}),
    ]
    expected = [ExpectedToolCall(tool="search_events")]
    precision, recall = trajectory_precision_recall(actual, expected)
    assert precision == 0.5
    assert recall == 1.0


def test_trajectory_recall_drops_when_expected_tool_missed() -> None:
    actual = [ToolCall(tool="search_events", arguments={})]
    expected = [
        ExpectedToolCall(tool="search_events"),
        ExpectedToolCall(tool="save_memory"),
    ]
    precision, recall = trajectory_precision_recall(actual, expected)
    assert precision == 1.0
    assert recall == 0.5


def test_trajectory_precision_recall_empty_case() -> None:
    # No expected, no actual → both 1.0 (accept the empty-set match).
    precision, recall = trajectory_precision_recall([], [])
    assert precision == 1.0
    assert recall == 1.0


# ---------------------------------------------------------------------------
# goal_completion_score
# ---------------------------------------------------------------------------


def test_goal_completion_delegates_to_judge() -> None:
    judge = _MockJudge([JudgeResponse(score=0.9, rationale="materially matches")])
    result = goal_completion_score(
        question="find tech events",
        expected_outcome="the answer names at least one tech event",
        actual_answer="I found the DevOps conference and Startup Night.",
        judge=judge,
        case_id="cheap-tech-weekend",
    )
    assert result.score == 0.9
    assert "materially matches" in result.rationale
    assert len(judge.calls) == 1


def test_goal_completion_shortcircuits_empty_answer() -> None:
    judge = _MockJudge([])  # would raise IndexError if called
    result = goal_completion_score(
        question="find tech events",
        expected_outcome="at least one tech event",
        actual_answer="",
        judge=judge,
        case_id="cheap-tech-weekend",
    )
    assert result.score == 0.0
    assert judge.calls == []


def test_goal_completion_cache_key_carries_case_id() -> None:
    judge = _MockJudge([JudgeResponse(score=0.75, rationale="partial")])
    goal_completion_score(
        question="q",
        expected_outcome="o",
        actual_answer="a",
        judge=judge,
        case_id="scenario-42",
    )
    _, cache_key = judge.calls[0]
    assert cache_key.case_id == "scenario-42"
    assert cache_key.metric == "goal_completion"
