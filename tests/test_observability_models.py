"""Unit tests for `planazo.observability.models`.

Locks the sanitizer (`format_stored_text`) and the aggregate
(`AgentRunRecord`) as two independent defenses — Rule 2 defense in
depth, same shape the scheduler bounded context uses for
`SchedulerRunRecord.errors` + `format_error_entry`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from planazo.observability.models import (
    FINAL_ANSWER_CAP,
    USER_QUERY_CAP,
    AgentRunRecord,
    format_stored_text,
)

# ---- format_stored_text ----------------------------------------------------


def test_format_stored_text_strips_c0_control_chars() -> None:
    """Every C0 control byte except tab becomes a space; whitespace runs collapse."""
    text = "hi\x00there\x01\x02friend"
    assert format_stored_text(text, 100) == "hi there friend"


def test_format_stored_text_strips_newlines_and_tabs_via_whitespace_collapse() -> None:
    """Tab lives through step 1 but step 2's `\\s+` collapse turns it into a space."""
    assert format_stored_text("one\nline\ttwo", 100) == "one line two"


def test_format_stored_text_strips_del_and_c1_controls() -> None:
    """DEL (0x7F) and every C1 (0x80-0x9F) are stripped along with the C0 range."""
    text = f"a{chr(0x7F)}b{chr(0x80)}c{chr(0x9F)}d"
    assert format_stored_text(text, 100) == "a b c d"


def test_format_stored_text_collapses_whitespace_runs_and_strips_edges() -> None:
    """Multiple spaces (or a mix of spaces + newlines) collapse to one space."""
    assert format_stored_text("   a   b   c   ", 100) == "a b c"


def test_format_stored_text_truncates_at_cap() -> None:
    """Truncation is on Unicode code points, not bytes."""
    text = "x" * 5000
    result = format_stored_text(text, 100)
    assert len(result) == 100
    assert result == "x" * 100


def test_format_stored_text_multibyte_truncation_stays_valid_utf8() -> None:
    """A multi-byte code point at the boundary is not split mid-byte."""
    text = "é" * 500  # 'é' — 2 UTF-8 bytes each; still one code point.
    result = format_stored_text(text, 100)
    assert len(result) == 100
    # Round-tripping through UTF-8 must not raise.
    result.encode("utf-8")


def test_format_stored_text_empty_input_returns_empty_string() -> None:
    assert format_stored_text("", 100) == ""


def test_format_stored_text_rejects_zero_cap() -> None:
    with pytest.raises(ValueError, match="cap must be >= 1"):
        format_stored_text("hi", 0)


def test_format_stored_text_rejects_negative_cap() -> None:
    with pytest.raises(ValueError, match="cap must be >= 1"):
        format_stored_text("hi", -1)


# ---- AgentRunRecord --------------------------------------------------------


def _record_kwargs(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": "run-1",
        "agent_kind": "recommender",
        "user_id": 1,
        "user_query": "find me techno events",
        "final_answer": "here are three",
        "stopped": "answered",
        "steps_count": 3,
        "started_at": now,
        "ended_at": now + timedelta(seconds=5),
    }
    base.update(overrides)
    return base


def test_agent_run_record_builds_from_valid_kwargs() -> None:
    record = AgentRunRecord(**_record_kwargs())
    assert record.run_id == "run-1"
    assert record.agent_kind == "recommender"
    assert record.stopped == "answered"


def test_agent_run_record_rejects_control_chars_in_user_query() -> None:
    """A caller who bypasses `format_stored_text` still fails at the boundary."""
    with pytest.raises(ValidationError, match="user_query must be sanitized"):
        AgentRunRecord(**_record_kwargs(user_query="hello\x00world"))


def test_agent_run_record_rejects_control_chars_in_final_answer() -> None:
    with pytest.raises(ValidationError, match="final_answer must be sanitized"):
        AgentRunRecord(**_record_kwargs(final_answer="bad\nnewline"))


def test_agent_run_record_accepts_none_final_answer() -> None:
    """`stopped='max_steps'` runs have `LoopResult.answer=None`."""
    record = AgentRunRecord(**_record_kwargs(final_answer=None, stopped="max_steps"))
    assert record.final_answer is None


def test_agent_run_record_accepts_null_user_id() -> None:
    """Operator-triggered runs may have no Telegram-user attribution."""
    record = AgentRunRecord(**_record_kwargs(user_id=None))
    assert record.user_id is None


@pytest.mark.parametrize("kind", ["recommender", "extractor"])
def test_agent_run_record_agent_kind_accepts_both_literals(kind: str) -> None:
    record = AgentRunRecord(**_record_kwargs(agent_kind=kind))
    assert record.agent_kind == kind


def test_agent_run_record_agent_kind_rejects_unknown_literal() -> None:
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(agent_kind="scheduler"))


@pytest.mark.parametrize("stopped", ["answered", "truncated", "max_steps"])
def test_agent_run_record_stopped_accepts_all_three_literals(stopped: str) -> None:
    record = AgentRunRecord(**_record_kwargs(stopped=stopped))
    assert record.stopped == stopped


def test_agent_run_record_stopped_rejects_preference_read_error() -> None:
    """`preference_read_error` never produces an `agent_runs` row — the
    composition root returns before the loop starts, so this Literal is
    absent from `AgentRunStopped` by design."""
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(stopped="preference_read_error"))


def test_agent_run_record_rejects_negative_steps_count() -> None:
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(steps_count=-1))


def test_agent_run_record_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(unexpected="oops"))


def test_agent_run_record_user_query_max_length_enforced() -> None:
    """Field-level max_length trips before the model_validator ever runs."""
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(user_query="a" * (USER_QUERY_CAP + 1)))


def test_agent_run_record_final_answer_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        AgentRunRecord(**_record_kwargs(final_answer="a" * (FINAL_ANSWER_CAP + 1)))


def test_agent_run_record_json_roundtrips() -> None:
    original = AgentRunRecord(**_record_kwargs())
    dumped = original.model_dump_json()
    restored = AgentRunRecord.model_validate_json(dumped)
    assert restored == original


def test_agent_run_record_sanitized_via_helper_passes_boundary() -> None:
    """`format_stored_text` output round-trips through `AgentRunRecord`.

    If this trip fires a ValidationError the helper's sanitization is
    not tight enough to satisfy the model's regex — that's the whole
    point of holding both to the same shape (defense in depth).
    """
    poisoned = "user asked:\ntell me about\ttechno\x00 events"
    sanitized = format_stored_text(poisoned, USER_QUERY_CAP)
    record = AgentRunRecord(**_record_kwargs(user_query=sanitized))
    assert record.user_query == sanitized
    assert "\n" not in record.user_query
    assert "\t" not in record.user_query
    assert "\x00" not in record.user_query
