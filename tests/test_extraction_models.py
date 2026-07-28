"""Contract tests for ``planazo.extraction.models.ExtractionResult``.

Locks the delegation hand-off invariant (status ↔ error_type ↔ event) and the
`notes` length cap that AGENTS.md Rule 2 depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.extraction.models import ExtractionResult


def _event() -> Event:
    return Event(
        source="instagram",
        source_url="https://www.instagram.com/p/abc123/",
        title="Test event",
        start_utc=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 1, 22, 0, tzinfo=UTC),
        category="music",
        city="Barcelona",
        confidence=0.9,
    )


def test_status_ok_with_event_and_no_error_type_accepts() -> None:
    result = ExtractionResult(status="ok", event=_event())
    assert result.status == "ok"
    assert result.event is not None
    assert result.error_type is None
    assert result.needs_approval is False


def test_status_ok_without_event_rejects() -> None:
    with pytest.raises(ValidationError, match="status='ok' requires event"):
        ExtractionResult(status="ok")


def test_status_ok_with_error_type_rejects() -> None:
    with pytest.raises(ValidationError, match="status='ok' forbids error_type"):
        ExtractionResult(status="ok", event=_event(), error_type="rate_limited")


def test_status_error_with_event_rejects() -> None:
    with pytest.raises(ValidationError, match="forbids event"):
        ExtractionResult(status="error", event=_event(), error_type="rate_limited")


def test_status_error_without_error_type_rejects() -> None:
    with pytest.raises(ValidationError, match="requires error_type"):
        ExtractionResult(status="error")


def test_status_needs_clarification_without_error_type_rejects() -> None:
    with pytest.raises(ValidationError, match="requires error_type"):
        ExtractionResult(status="needs_clarification")


def test_status_needs_clarification_with_error_type_accepts() -> None:
    result = ExtractionResult(
        status="needs_clarification",
        error_type="ambiguous_content",
        notes="two dates in caption",
    )
    assert result.event is None
    assert result.error_type == "ambiguous_content"


def test_needs_approval_true_rejects() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(
            {
                "status": "ok",
                "event": _event().model_dump(mode="json"),
                "needs_approval": True,
            }
        )


def test_notes_at_max_length_accepts() -> None:
    result = ExtractionResult(
        status="error",
        error_type="rate_limited",
        notes="x" * 200,
    )
    assert len(result.notes) == 200


def test_notes_over_max_length_rejects() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult(
            status="error",
            error_type="rate_limited",
            notes="x" * 201,
        )


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(
            {
                "status": "ok",
                "event": _event().model_dump(mode="json"),
                "surprise": "extra",
            }
        )


def test_model_dump_json_round_trip_is_lossless() -> None:
    original = ExtractionResult(status="ok", event=_event(), notes="parsed cleanly")
    encoded = original.model_dump_json()
    round_tripped = ExtractionResult.model_validate_json(encoded)
    assert round_tripped == original


def test_error_result_json_round_trip_is_lossless() -> None:
    original = ExtractionResult(
        status="needs_clarification",
        error_type="multiple_events_in_post",
        notes="three shows announced in a single carousel",
    )
    round_tripped = ExtractionResult.model_validate_json(original.model_dump_json())
    assert round_tripped == original
