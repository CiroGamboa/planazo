"""Contract tests for ``planazo.extraction.models.ExtractionResult``.

Locks the delegation hand-off invariant (status ↔ events ↔ error_type) and
the `notes` length cap that AGENTS.md Rule 2 depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.extraction.models import ExtractionResult


def _event(source_url: str = "https://www.instagram.com/p/abc123/", **overrides: object) -> Event:
    defaults: dict[str, object] = {
        "source": "instagram",
        "source_url": source_url,
        "title": "Test event",
        "start_utc": datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 22, 0, tzinfo=UTC),
        "category": "music",
        "city": "Barcelona",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


def test_status_ok_with_one_event_and_no_error_type_accepts() -> None:
    result = ExtractionResult(status="ok", events=[_event()])
    assert result.status == "ok"
    assert len(result.events) == 1
    assert result.error_type is None
    assert result.needs_approval is False


def test_status_ok_with_two_events_accepts_and_preserves_order() -> None:
    first = _event(title="First event", event_index_in_post=0)
    second = _event(title="Second event", event_index_in_post=1)

    result = ExtractionResult(status="ok", events=[first, second])

    assert len(result.events) == 2
    assert result.events[0].title == "First event"
    assert result.events[0].event_index_in_post == 0
    assert result.events[1].title == "Second event"
    assert result.events[1].event_index_in_post == 1


def test_status_ok_with_empty_events_rejects() -> None:
    with pytest.raises(ValidationError, match="status='ok' requires at least one event"):
        ExtractionResult(status="ok")


def test_status_ok_with_error_type_rejects() -> None:
    with pytest.raises(ValidationError, match="status='ok' requires error_type=None"):
        ExtractionResult(status="ok", events=[_event()], error_type="rate_limited")


def test_status_error_with_events_rejects() -> None:
    with pytest.raises(ValidationError, match="requires events=\\[\\]"):
        ExtractionResult(status="error", events=[_event()], error_type="rate_limited")


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
    assert result.events == []
    assert result.error_type == "ambiguous_content"


def test_needs_approval_true_rejects() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(
            {
                "status": "ok",
                "events": [_event().model_dump(mode="json")],
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
                "events": [_event().model_dump(mode="json")],
                "surprise": "extra",
            }
        )


def test_model_dump_json_round_trip_is_lossless_for_one_event() -> None:
    original = ExtractionResult(status="ok", events=[_event()], notes="parsed cleanly")
    encoded = original.model_dump_json()
    round_tripped = ExtractionResult.model_validate_json(encoded)
    assert round_tripped == original


def test_model_dump_json_round_trip_is_lossless_for_two_events() -> None:
    first = _event(title="First event", event_index_in_post=0)
    second = _event(title="Second event", event_index_in_post=1)
    original = ExtractionResult(status="ok", events=[first, second], notes="two saved")

    encoded = original.model_dump_json()
    round_tripped = ExtractionResult.model_validate_json(encoded)

    assert round_tripped == original
    assert [event.title for event in round_tripped.events] == ["First event", "Second event"]


def test_error_result_json_round_trip_is_lossless() -> None:
    original = ExtractionResult(
        status="needs_clarification",
        error_type="multiple_events_in_post",
        notes="three shows announced in a single carousel",
    )
    round_tripped = ExtractionResult.model_validate_json(original.model_dump_json())
    assert round_tripped == original
