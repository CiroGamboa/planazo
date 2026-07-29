"""Unit tests for `planazo.scheduler.models`.

Covers ScanState/SchedulerRunRecord/TickReport shape + the format_error_entry
helper. The helper + the model-level regex validator are two independent
defenses against the Rule-2 leak channel (Instagram caption bytes bleeding
into `SchedulerRunRecord.errors` via a wrapped exception message) — each is
tested standalone so a future refactor cannot silently drop one and pass
the suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from planazo.scheduler.models import (
    TRUNCATE_LEN,
    ScanState,
    SchedulerRunRecord,
    TickReport,
    format_error_entry,
)

# ---- ScanState -------------------------------------------------------------


def test_scan_state_rejects_negative_consecutive_failures() -> None:
    with pytest.raises(ValidationError):
        ScanState(source_url="https://www.instagram.com/p/A/", consecutive_failures=-1)


def test_scan_state_null_last_scanned_at_and_last_success_at_are_valid() -> None:
    state = ScanState(source_url="https://www.instagram.com/p/A/")
    assert state.last_scanned_at is None
    assert state.last_success_at is None
    assert state.consecutive_failures == 0


def test_scan_state_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        ScanState(source_url="https://www.instagram.com/p/A/", unexpected="oops")  # type: ignore[call-arg]


def test_scan_state_rejects_empty_source_url() -> None:
    with pytest.raises(ValidationError):
        ScanState(source_url="")


# ---- SchedulerRunRecord ----------------------------------------------------


def _run_record_kwargs(**overrides: object) -> dict[str, object]:
    """Shared factory — a valid `SchedulerRunRecord` you can override piece-wise."""
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": "run-1",
        "source_url": "https://www.instagram.com/p/A/",
        "source_kind": "post",
        "backend": None,
        "gate_reason": "first_run",
        "posts_discovered": 0,
        "posts_extracted_ok": 1,
        "posts_extracted_error": 0,
        "posts_skipped_idempotent": 0,
        "errors": [],
        "started_at": now,
        "ended_at": now + timedelta(seconds=2),
    }
    base.update(overrides)
    return base


def test_post_source_kind_requires_backend_none() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**_run_record_kwargs(source_kind="post", backend="anonymous"))


def test_account_source_kind_requires_backend_populated() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(
            **_run_record_kwargs(
                source_kind="account",
                backend=None,
                source_url="https://www.instagram.com/curated.agenda/",
            )
        )


def test_account_source_kind_accepts_populated_backend() -> None:
    record = SchedulerRunRecord(
        **_run_record_kwargs(
            source_kind="account",
            backend="hikerapi",
            source_url="https://www.instagram.com/curated.agenda/",
            gate_reason="cadence_ready",
        )
    )
    assert record.backend == "hikerapi"


def test_record_rejects_negative_counters() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**_run_record_kwargs(posts_discovered=-1))


def test_record_requires_gate_reason() -> None:
    kwargs = _run_record_kwargs()
    kwargs.pop("gate_reason")
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**kwargs)


@pytest.mark.parametrize(
    "reason",
    ["first_run", "cadence_ready", "cadence_not_ready", "failure_skip"],
)
def test_record_gate_reason_accepts_all_four_literals(reason: str) -> None:
    record = SchedulerRunRecord(**_run_record_kwargs(gate_reason=reason))
    assert record.gate_reason == reason


def test_record_gate_reason_rejects_unknown_literal() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**_run_record_kwargs(gate_reason="what"))


def test_record_errors_entry_must_match_regex() -> None:
    # Free-form entries — no `error_type:` prefix — are rejected at the boundary
    # regardless of how the caller assembled them (Rule 2 defense in depth).
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**_run_record_kwargs(errors=["oops"]))

    # A properly-shaped entry validates.
    record = SchedulerRunRecord(**_run_record_kwargs(errors=["rate_limited: hikerapi 429"]))
    assert record.errors == ["rate_limited: hikerapi 429"]


def test_record_errors_entry_rejects_embedded_newline() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(
            **_run_record_kwargs(errors=["rate_limited: oops\nsecret caption"]),
        )


def test_record_errors_entry_rejects_embedded_tab() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(
            **_run_record_kwargs(errors=["rate_limited: a\tb"]),
        )


def test_record_json_roundtrips() -> None:
    original = SchedulerRunRecord(**_run_record_kwargs(errors=["rate_limited: hikerapi 429"]))
    dumped = original.model_dump_json()
    restored = SchedulerRunRecord.model_validate_json(dumped)
    assert restored == original


def test_record_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        SchedulerRunRecord(**_run_record_kwargs(unexpected="x"))


# ---- format_error_entry ----------------------------------------------------


def test_format_error_entry_produces_canonical_shape() -> None:
    assert format_error_entry("rate_limited", "hikerapi 429") == "rate_limited: hikerapi 429"


def test_format_error_entry_truncates_at_truncate_len() -> None:
    entry = format_error_entry("rate_limited", "x" * 500)
    prefix = "rate_limited: "
    assert entry.startswith(prefix)
    assert len(entry) - len(prefix) == TRUNCATE_LEN


def test_format_error_entry_strips_newlines() -> None:
    entry = format_error_entry("rate_limited", "first\nsecond")
    assert entry == "rate_limited: first second"


def test_format_error_entry_strips_tabs() -> None:
    entry = format_error_entry("rate_limited", "a\tb")
    assert entry == "rate_limited: a b"


def test_format_error_entry_collapses_internal_whitespace() -> None:
    entry = format_error_entry("rate_limited", "a   b")
    assert entry == "rate_limited: a b"


def test_format_error_entry_rejects_unknown_error_type() -> None:
    with pytest.raises(ValueError):
        format_error_entry("bogus", "detail")  # type: ignore[arg-type]


def test_format_error_entry_prevents_caption_leak() -> None:
    # The exact leak channel #64 named for `notes`: a wrapped exception message
    # quotes the Instagram caption verbatim, then a caller `str(exc)` splats it
    # into the audit log. The helper truncates + strips newlines so a caption
    # cannot ride past the 120-char detail window and cannot split the JSONL
    # entry across two lines.
    poisoned = (
        "429 for user @business_venue with caption: come see us at C/ Balmes 12 "
        "for our Friday night set — DJ Serdlic playing until 4am, drinks 2x1"
    )
    entry = format_error_entry("rate_limited", poisoned)
    prefix = "rate_limited: "
    assert entry.startswith(prefix)
    assert len(entry) == len(prefix) + TRUNCATE_LEN
    assert "\n" not in entry
    assert "\t" not in entry


def test_format_error_entry_output_passes_scheduler_run_record_regex() -> None:
    poisoned = "some detail\nfrom a wrapped exception\twith a tab"
    entry = format_error_entry("rate_limited", poisoned)
    # If this round-trip fires a ValidationError the helper's sanitization is
    # not tight enough to satisfy the model's regex — that's the whole point
    # of holding both the helper and the model to the same shape.
    record = SchedulerRunRecord(**_run_record_kwargs(errors=[entry]))
    assert record.errors == [entry]


def test_format_error_entry_empty_detail_still_matches_regex() -> None:
    entry = format_error_entry("rate_limited", "")
    assert entry == "rate_limited: "
    # And the model accepts it.
    record = SchedulerRunRecord(**_run_record_kwargs(errors=[entry]))
    assert record.errors == [entry]


# ---- TickReport ------------------------------------------------------------


def test_tick_report_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        TickReport(records=[], total_events_extracted=0, wall_clock_ms=0, unexpected=1)  # type: ignore[call-arg]


def test_tick_report_rejects_negative_wall_clock_ms() -> None:
    with pytest.raises(ValidationError):
        TickReport(records=[], total_events_extracted=0, wall_clock_ms=-1)


def test_tick_report_default_empty_records() -> None:
    report = TickReport(total_events_extracted=0, wall_clock_ms=0)
    assert report.records == []
