from typing import Literal

from tools.schema import schema_for
from tools.tools import confirm_and_create_calendar_event, save_event_candidate


def add(a: float, b: float) -> float:
    """Add exactly two numbers and return their sum."""
    return a + b


def choose(
    kind: Literal["movie", "show", "game"], title: str, runtime_min: int
) -> dict[str, object]:
    """Pretend tool exercising every annotation kind schema_for should handle."""
    return {}


def test_schema_for_derives_name_description_and_required() -> None:
    schema = schema_for(add)

    assert schema["type"] == "function"
    assert schema["name"] == "add"
    assert schema["description"] == "Add exactly two numbers and return their sum."
    assert schema["parameters"]["properties"] == {
        "a": {"type": "number"},
        "b": {"type": "number"},
    }
    assert schema["parameters"]["required"] == ["a", "b"]
    assert schema["parameters"]["additionalProperties"] is False


def test_schema_for_translates_literal_annotation_to_enum() -> None:
    schema = schema_for(choose)

    assert schema["parameters"]["properties"]["kind"] == {
        "type": "string",
        "enum": ["movie", "show", "game"],
    }
    assert schema["parameters"]["properties"]["title"] == {"type": "string"}
    assert schema["parameters"]["properties"]["runtime_min"] == {"type": "integer"}
    assert schema["parameters"]["required"] == ["kind", "title", "runtime_min"]


def test_save_event_candidate_schema_has_constrained_enum_parameters() -> None:
    schema = schema_for(save_event_candidate)

    assert schema["name"] == "save_event_candidate"
    assert schema["parameters"]["properties"]["category"] == {
        "type": "string",
        "enum": ["tech", "cultural", "music", "networking", "sports", "other"],
    }
    assert schema["parameters"]["properties"]["source"] == {
        "type": "string",
        "enum": ["eventbrite", "meetup", "instagram", "manual"],
    }
    assert schema["parameters"]["required"] == [
        "event_id",
        "title",
        "category",
        "source",
        "start_time",
        "location",
        "confidence",
    ]
    description = schema["description"].lower()
    assert "call this after" in description
    assert "do not call" in description


def test_confirm_and_create_calendar_event_schema_has_constrained_params() -> None:
    schema = schema_for(confirm_and_create_calendar_event)

    assert schema["name"] == "confirm_and_create_calendar_event"
    assert schema["parameters"]["properties"]["notify_invitees"] == {
        "type": "string",
        "enum": ["none", "email_invite"],
    }
    assert schema["parameters"]["required"] == ["event_id", "notify_invitees"]
    assert "invitee_emails" in schema["parameters"]["properties"]
    description = schema["description"].lower()
    assert "call this only after" in description
    assert "do not call" in description
