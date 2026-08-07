"""Unit tests for the HW4 tracing helpers in `planazo.observability.tracing`.

Covers the pure pieces — `estimate_tokens` (tiktoken fallback shape) and
the tag-helper "no active trace" degrade path. `configure_tracing` and
the wrappers that touch `mlflow.update_current_trace` are exercised
indirectly by `tests/eval/agent/test_metrics.py` and the real
end-to-end run committed under `data/eval/results/`.
"""

from __future__ import annotations

from planazo.observability.tracing import (
    estimate_tokens,
    set_agent_kind,
    set_eval_case_id,
    set_request_origin,
    set_token_usage,
)


def test_estimate_tokens_returns_int() -> None:
    assert isinstance(estimate_tokens("hello world"), int)


def test_estimate_tokens_grows_with_text_length() -> None:
    short = estimate_tokens("hi")
    long = estimate_tokens("hi " * 100)
    assert long > short


def test_estimate_tokens_zero_for_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_set_request_origin_no_active_trace_is_a_noop() -> None:
    # No active mlflow trace → the fluent update raises internally;
    # the helper must swallow it. Absence of a raise is the assertion.
    set_request_origin("cli")


def test_set_eval_case_id_no_active_trace_is_a_noop() -> None:
    set_eval_case_id("smoke-case")


def test_set_agent_kind_no_active_trace_is_a_noop() -> None:
    set_agent_kind("recommender")


def test_set_token_usage_no_active_trace_is_a_noop() -> None:
    set_token_usage("some input text", "some output text")
