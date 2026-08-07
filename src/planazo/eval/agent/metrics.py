"""Trajectory + goal-completion scorers for the agent-eval harness.

Three metrics, all pure over the inputs the runner + adapter surface:

- ``tool_selection_accuracy`` — for each expected tool, does at least one
  observed tool call match name + subset args? Returns matched / max(1,
  len(expected)). Denominator's ``max(1, ...)`` keeps a 0-expected case
  (e.g. preflight abort) at score 1.0 when no calls happen.
- ``trajectory_precision_recall`` — position-independent set match.
  Precision = matched / max(1, actual), recall = matched / max(1,
  expected).
- ``goal_completion_score`` — LLM-as-judge over ``expected_outcome``
  vs ``actual_answer``. Reuses ``LLMJudge`` + ``JudgeCacheKey`` from
  ``planazo.eval.judge`` so a rerun of the harness is free.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from planazo.eval.agent.models import ExpectedToolCall, ToolCall
from planazo.eval.judge import (
    JudgeCacheKey,
    JudgeResponse,
    LLMJudge,
    compute_answer_hash,
)

_MAX_ANSWER_CHARS: Final[int] = 2000
_PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(template_name: str) -> str:
    """Read one of the committed ``eval/prompts/*.md`` templates as a string."""
    return (_PROMPTS_DIR / template_name).read_text(encoding="utf-8")


def _render(template: str, variables: dict[str, str]) -> str:
    """Substitute every ``{{ name }}`` placeholder with ``variables[name]``.

    Plain ``str.replace`` — safer than ``str.format`` against any ``{``
    character that might appear inside a question, answer, or expected
    outcome string.
    """
    rendered = template
    for name, value in variables.items():
        rendered = rendered.replace("{{ " + name + " }}", value)
    return rendered


def _matches(expected: ExpectedToolCall, actual: ToolCall) -> bool:
    """Return ``True`` iff ``actual`` matches ``expected``'s name and args."""
    if expected.tool != actual.tool:
        return False
    return actual.args_contains(expected.args_contains)


def tool_selection_accuracy(
    actual: Sequence[ToolCall],
    expected: Sequence[ExpectedToolCall],
) -> float:
    """Fraction of ``expected`` tool calls that were satisfied by ``actual``.

    Each expected tool is either matched (at least one actual call has
    the same name and covers the declared subset of arguments) or
    unmatched. The result is ``matched / max(1, len(expected))`` so an
    empty expectation yields ``1.0`` when ``actual`` is also empty and
    still ``1.0`` when it is not — the metric measures whether the
    expected calls happened, not whether extra ones did.
    """
    if not expected:
        return 1.0
    matched = 0
    for expectation in expected:
        if any(_matches(expectation, call) for call in actual):
            matched += 1
    return matched / max(1, len(expected))


def trajectory_precision_recall(
    actual: Sequence[ToolCall],
    expected: Sequence[ExpectedToolCall],
) -> tuple[float, float]:
    """Position-independent precision + recall over the tool trajectory.

    Precision = ``matched / max(1, len(actual))``: how many of the calls
    the model made were on the expected list. Recall = ``matched /
    max(1, len(expected))``: how many of the expected calls the model
    actually made. ``max(1, ...)`` avoids a divide-by-zero when either
    side is empty. When both sides are empty, both scores are ``1.0``
    (the trivial-agreement case).
    """
    if not actual and not expected:
        return 1.0, 1.0
    matched_expected = 0
    for expectation in expected:
        if any(_matches(expectation, call) for call in actual):
            matched_expected += 1
    matched_actual = 0
    for call in actual:
        if any(_matches(expectation, call) for expectation in expected):
            matched_actual += 1
    precision = matched_actual / max(1, len(actual))
    recall = matched_expected / max(1, len(expected))
    return precision, recall


def goal_completion_score(
    *,
    question: str,
    expected_outcome: str,
    actual_answer: str,
    judge: LLMJudge,
    case_id: str,
) -> JudgeResponse:
    """LLM-as-judge score of ``actual_answer`` against ``expected_outcome``.

    Empty answers short-circuit to ``JudgeResponse(score=0.0, ...)``
    without touching the model — an empty answer cannot satisfy an
    expected outcome. Anything longer is bounded at
    ``_MAX_ANSWER_CHARS`` to defuse the long-context bias that lets a
    rambling answer sway the judge simply by taking more tokens.

    Delegates to ``judge.judge`` through a stable ``JudgeCacheKey`` — the
    scorer reuses the same disk-cache layer as the HW3 generation
    scorers, so a rerun on unchanged inputs is free.
    """
    if not actual_answer.strip():
        return JudgeResponse(
            score=0.0,
            rationale="empty_answer: nothing to score against expected outcome",
        )
    bounded_answer = actual_answer[:_MAX_ANSWER_CHARS]
    prompt = _render(
        _load_prompt("goal_completion.md"),
        {
            "question": question,
            "expected_outcome": expected_outcome,
            "actual_answer": bounded_answer,
        },
    )
    cache_key = JudgeCacheKey(
        metric="goal_completion",
        case_id=case_id,
        answer_hash=compute_answer_hash([question, expected_outcome, bounded_answer]),
    )
    return judge.judge(prompt, cache_key=cache_key)


__all__ = [
    "goal_completion_score",
    "tool_selection_accuracy",
    "trajectory_precision_recall",
]
