import json
from pathlib import Path

import pytest

from tools.tools import confirm_and_create_calendar_event, save_event_candidate


@pytest.fixture(autouse=True)
def _redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.tools.CANDIDATES_PATH", tmp_path / "candidates.json")
    monkeypatch.setattr("tools.tools.CALENDAR_EVENTS_PATH", tmp_path / "calendar_events.json")


def test_save_event_candidate_persists_and_verifies(tmp_path: Path) -> None:
    first = save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "2026-08-01T19:00:00", "Barcelona", 0.9
    )
    assert first["saved"] == {
        "id": 1,
        "event_id": "evt-1",
        "title": "AI Meetup",
        "category": "tech",
        "source": "meetup",
        "start_time": "2026-08-01T19:00:00",
        "location": "Barcelona",
        "confidence": 0.9,
    }
    assert first["total_candidates"] == 1

    second = save_event_candidate(
        "evt-2", "Jazz Night", "music", "instagram", "2026-08-02T21:00:00", "Barcelona", 0.7
    )
    assert second["saved"]["event_id"] == "evt-2"
    assert second["total_candidates"] == 2

    # VERIFY: what's actually on disk matches what the tool reported back.
    on_disk = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))
    assert len(on_disk) == 2
    assert on_disk[0]["event_id"] == "evt-1"
    assert on_disk[1]["event_id"] == "evt-2"


def test_save_event_candidate_rejects_an_unparseable_start_time(tmp_path: Path) -> None:
    result = save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "not-a-date", "Barcelona", 0.9
    )

    assert result["error_type"] == "invalid_event_data"
    assert not (tmp_path / "candidates.json").exists()


def test_save_event_candidate_rejects_low_confidence_extraction(tmp_path: Path) -> None:
    result = save_event_candidate(
        "evt-1", "Maybe a Meetup?", "tech", "instagram", "2026-08-01T19:00:00", "Barcelona", 0.1
    )

    assert result == {
        "error_type": "low_confidence_extraction",
        "message": "confidence 0.1 is below 0.3; not reliable enough to save",
    }
    assert not (tmp_path / "candidates.json").exists()


def test_confirm_and_create_calendar_event_persists_and_verifies(tmp_path: Path) -> None:
    save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "2026-08-01T19:00:00", "Barcelona", 0.9
    )

    result = confirm_and_create_calendar_event("evt-1", "none")

    assert result["created"] == {
        "id": 1,
        "event_id": "evt-1",
        "title": "AI Meetup",
        "start_time": "2026-08-01T19:00:00",
        "location": "Barcelona",
        "notify_invitees": "none",
        "invitee_emails": [],
    }
    assert result["total_confirmed"] == 1
    assert result["invitees_notified"] is False

    # VERIFY: what's actually on disk matches what the tool reported back.
    on_disk = json.loads((tmp_path / "calendar_events.json").read_text(encoding="utf-8"))
    assert len(on_disk) == 1
    assert on_disk[0]["event_id"] == "evt-1"


def test_confirm_and_create_calendar_event_notifies_invitees(tmp_path: Path) -> None:
    save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "2026-08-01T19:00:00", "Barcelona", 0.9
    )

    result = confirm_and_create_calendar_event(
        "evt-1", "email_invite", "a@example.com, b@example.com"
    )

    assert result["created"]["invitee_emails"] == ["a@example.com", "b@example.com"]
    assert result["invitees_notified"] is True


def test_confirm_and_create_calendar_event_requires_invitees_when_notifying(
    tmp_path: Path,
) -> None:
    save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "2026-08-01T19:00:00", "Barcelona", 0.9
    )

    result = confirm_and_create_calendar_event("evt-1", "email_invite", "")

    assert result["error_type"] == "missing_invitees"
    assert not (tmp_path / "calendar_events.json").exists()


def test_confirm_and_create_calendar_event_rejects_a_malformed_email(tmp_path: Path) -> None:
    save_event_candidate(
        "evt-1", "AI Meetup", "tech", "meetup", "2026-08-01T19:00:00", "Barcelona", 0.9
    )

    result = confirm_and_create_calendar_event("evt-1", "email_invite", "not-an-email")

    assert result["error_type"] == "invalid_confirmation_data"
    assert not (tmp_path / "calendar_events.json").exists()


def test_confirm_and_create_calendar_event_reports_event_not_found(tmp_path: Path) -> None:
    result = confirm_and_create_calendar_event("evt-does-not-exist", "none")

    assert result == {
        "error_type": "event_not_found",
        "message": "no saved candidate with event_id 'evt-does-not-exist'",
    }
    assert not (tmp_path / "calendar_events.json").exists()


def test_tool_registry_and_irreversible_set() -> None:
    from tools.tools import IRREVERSIBLE_TOOLS, TOOL_REGISTRY

    assert set(TOOL_REGISTRY) == {"save_event_candidate", "confirm_and_create_calendar_event"}
    assert {"confirm_and_create_calendar_event"} == IRREVERSIBLE_TOOLS
    assert "save_event_candidate" not in IRREVERSIBLE_TOOLS
