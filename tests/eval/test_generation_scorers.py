"""Unit tests for the LLM-as-judge cache + the three generation scorers.

Every test uses a `MockJudge` that returns scripted `JudgeResponse` values
so no live model call ever leaves the test. The cache behaviour tests hit
the real on-disk cache under `tmp_path`, exercising the same read/write
plumbing the harness uses in production.

The scorers are thin wrappers over `LLMJudge.judge`, so the tests focus on
two invariants: (1) the scorer returns the judge's response unchanged, and
(2) the disk cache short-circuits repeat calls with the same key.
"""

from __future__ import annotations

import json
from pathlib import Path

from planazo.eval.judge import (
    JudgeCacheKey,
    JudgeResponse,
    OpenCodeJudge,
    compute_answer_hash,
    read_cached_response,
    write_cached_response,
)
from planazo.eval.metrics.generation import (
    score_answer_relevance,
    score_context_precision,
    score_faithfulness,
)


class MockJudge:
    """Scripted `LLMJudge` — pops from `responses` on each `.judge()` call.

    Records every call in `self.calls` so tests can assert both the returned
    score and the fact that the judge was (or was not) invoked. If
    `responses` is exhausted a test failure surfaces immediately rather
    than any silent fallback.
    """

    def __init__(self, responses: list[JudgeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, JudgeCacheKey]] = []

    def judge(self, prompt: str, *, cache_key: JudgeCacheKey) -> JudgeResponse:
        self.calls.append((prompt, cache_key))
        if not self.responses:
            raise AssertionError("MockJudge ran out of scripted responses")
        return self.responses.pop(0)


def test_score_faithfulness_returns_high_score_when_judge_returns_high() -> None:
    judge = MockJudge([JudgeResponse(score=0.9, rationale="all claims supported")])

    result = score_faithfulness(
        answer="Palau Dalmases hosts flamenco tonight.",
        chunks=["Flamenco tablao at Palau Dalmases, El Born, 21:00."],
        judge=judge,
        case_id="q001",
    )

    assert result.score == 0.9
    assert judge.calls[0][1].metric == "faithfulness"
    assert judge.calls[0][1].case_id == "q001"


def test_score_faithfulness_returns_low_score_when_judge_returns_low() -> None:
    judge = MockJudge([JudgeResponse(score=0.1, rationale="answer invents a venue")])

    result = score_faithfulness(
        answer="Palau Dalmases hosts an opera festival.",
        chunks=["Flamenco tablao at Palau Dalmases, El Born, 21:00."],
        judge=judge,
        case_id="q001",
    )

    assert result.score == 0.1


def test_score_answer_relevance_returns_high_score_when_judge_returns_high() -> None:
    judge = MockJudge([JudgeResponse(score=0.95, rationale="direct answer")])

    result = score_answer_relevance(
        query="flamenco shows in the Gothic Quarter tonight",
        answer="Palau Dalmases hosts a flamenco tablao at 21:00 in El Born.",
        judge=judge,
        case_id="q001",
    )

    assert result.score == 0.95
    assert judge.calls[0][1].metric == "answer_relevance"


def test_score_answer_relevance_returns_low_score_when_judge_returns_low() -> None:
    judge = MockJudge([JudgeResponse(score=0.05, rationale="off-topic")])

    result = score_answer_relevance(
        query="flamenco shows in the Gothic Quarter tonight",
        answer="The metro runs until midnight.",
        judge=judge,
        case_id="q001",
    )

    assert result.score == 0.05


def test_score_context_precision_returns_high_score_when_judge_returns_high() -> None:
    judge = MockJudge([JudgeResponse(score=0.8, rationale="4 of 5 chunks on-topic")])

    result = score_context_precision(
        query="startup pitch nights this week",
        chunks=[
            "Startup pitch night at Palau Robert.",
            "Y Combinator alumni panel at Barcelona Tech Hub.",
        ],
        judge=judge,
        case_id="q003",
    )

    assert result.score == 0.8
    assert judge.calls[0][1].metric == "context_precision"


def test_score_context_precision_returns_low_score_when_judge_returns_low() -> None:
    judge = MockJudge([JudgeResponse(score=0.0, rationale="none relevant")])

    result = score_context_precision(
        query="startup pitch nights this week",
        chunks=[
            "Yoga class in Poble Sec.",
            "Ceramic workshop in Sant Antoni.",
        ],
        judge=judge,
        case_id="q003",
    )

    assert result.score == 0.0


def test_judge_cache_hit_skips_llm_call(tmp_path: Path) -> None:
    cache_root = tmp_path / "judge_cache"
    key = JudgeCacheKey(
        metric="faithfulness",
        case_id="q001",
        answer_hash=compute_answer_hash(["some answer", "some chunk sig"]),
    )
    # Pre-populate the cache so a cache-hit path can be exercised.
    write_cached_response(
        cache_root,
        key,
        JudgeResponse(score=0.42, rationale="cached rationale"),
        prompt_hash="deadbeef",
    )

    # `enabled=False` prevents any live call; the cache read alone must return the value.
    judge = OpenCodeJudge(cache_root=cache_root, enabled=False)
    response = judge.judge("prompt does not matter — cache hit", cache_key=key)

    assert response.score == 0.42
    assert response.rationale == "cached rationale"


def test_judge_cache_miss_writes_response(tmp_path: Path) -> None:
    cache_root = tmp_path / "judge_cache"
    judge = MockJudge([JudgeResponse(score=0.7, rationale="grounded")])

    result = score_faithfulness(
        answer="Palau Dalmases hosts flamenco tonight.",
        chunks=["Flamenco tablao at Palau Dalmases, 21:00."],
        judge=judge,
        case_id="q001",
    )
    # The scorer must have consulted the judge — no cache exists yet.
    assert len(judge.calls) == 1
    # Persist the response to the same cache layout the real judge uses so
    # the reader-side round-trip is exercised end-to-end.
    write_cached_response(
        cache_root,
        judge.calls[0][1],
        result,
        prompt_hash="deadbeef",
    )

    key = judge.calls[0][1]
    cache_path = cache_root / key.metric / key.case_id / f"{key.answer_hash}.json"
    assert cache_path.exists(), "expected cache file to land on disk"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["score"] == 0.7
    assert payload["rationale"] == "grounded"

    round_tripped = read_cached_response(cache_root, key)
    assert round_tripped is not None
    assert round_tripped.score == 0.7


def test_judge_parse_failure_returns_score_zero_with_rationale(tmp_path: Path) -> None:
    cache_root = tmp_path / "judge_cache"
    key = JudgeCacheKey(
        metric="faithfulness",
        case_id="q001",
        answer_hash="deadbeef00000000",
    )

    # `enabled=False` + no pre-populated cache → the safe fallback path.
    judge = OpenCodeJudge(cache_root=cache_root, enabled=False)
    response = judge.judge("prompt", cache_key=key)

    assert response.score == 0.0
    assert response.rationale.startswith("judge_parse_failed:")
    # The fallback also caches, so a rerun does not repeat the wasted call.
    cached = read_cached_response(cache_root, key)
    assert cached is not None
    assert cached.score == 0.0


def test_enabled_judge_retries_once_then_falls_back_on_repeated_bad_json(
    tmp_path: Path,
) -> None:
    """Enabled judge: first `_invoke` returns garbage → retry with stricter reprompt →
    still garbage → typed `judge_parse_failed` fallback, cached."""

    cache_root = tmp_path / "judge_cache"
    key = JudgeCacheKey(
        metric="answer_relevance",
        case_id="q042",
        answer_hash="cafebabecafebabe",
    )

    calls: list[bool] = []

    def _fake_invoke(_self: OpenCodeJudge, prompt: str, *, stricter: bool) -> str:
        calls.append(stricter)
        return "this is not JSON, please score me: seven"

    judge = OpenCodeJudge(cache_root=cache_root, enabled=True)
    # Bypass the real ChatOpenAI construction — the retry branch is what we
    # care about, and it lives entirely in the OpenCodeJudge itself.
    judge._chat_model = object()  # type: ignore[assignment]  # sentinel, never used
    OpenCodeJudge._invoke = _fake_invoke  # type: ignore[method-assign]
    try:
        response = judge.judge("does not matter", cache_key=key)
    finally:
        # Restore the real method on the class so later tests are unaffected.
        del OpenCodeJudge._invoke  # type: ignore[method-assign]

    # Both attempts were made — first non-strict, then stricter.
    assert calls == [False, True]
    assert response.score == 0.0
    assert response.rationale.startswith("judge_parse_failed:")

    # The typed fallback was cached so a rerun does not repeat the pair of calls.
    cached = read_cached_response(cache_root, key)
    assert cached is not None
    assert cached.score == 0.0
    assert cached.rationale.startswith("judge_parse_failed:")


def test_enabled_judge_refuses_prompt_over_token_budget(tmp_path: Path) -> None:
    """Enabled judge: prompt above ``_MAX_PROMPT_TOKENS`` short-circuits to a
    typed fallback without any LLM call — the tiktoken guard fires here."""

    from planazo.eval.judge import count_prompt_tokens

    cache_root = tmp_path / "judge_cache"
    key = JudgeCacheKey(
        metric="context_precision",
        case_id="q999",
        answer_hash="badf00dbadf00d00",
    )

    # 40 000 tokens of the same word — well over the 8 000-token guard.
    oversize_prompt = ("token " * 40_000).strip()
    assert count_prompt_tokens(oversize_prompt) > 8_000

    invoked = 0

    def _fake_invoke(_self: OpenCodeJudge, prompt: str, *, stricter: bool) -> str:
        nonlocal invoked
        invoked += 1
        return '{"score": 1.0, "rationale": "should not be reached"}'

    judge = OpenCodeJudge(cache_root=cache_root, enabled=True)
    judge._chat_model = object()  # type: ignore[assignment]
    OpenCodeJudge._invoke = _fake_invoke  # type: ignore[method-assign]
    try:
        response = judge.judge(oversize_prompt, cache_key=key)
    finally:
        del OpenCodeJudge._invoke  # type: ignore[method-assign]

    assert invoked == 0, "oversize prompt must not reach the LLM"
    assert response.score == 0.0
    assert response.rationale.startswith("judge_over_token_budget:")

    # Fallback is cached so subsequent runs stay cheap.
    cached = read_cached_response(cache_root, key)
    assert cached is not None
    assert cached.rationale.startswith("judge_over_token_budget:")
