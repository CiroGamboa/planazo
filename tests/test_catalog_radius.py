from datetime import UTC, datetime
from inspect import getsource, signature

import pytest
from pydantic import ValidationError

from planazo.catalog import Event, filter_events_for_intent, save_event
from planazo.catalog.radius import _haversine_km
from planazo.query import SearchIntent, SearchOrigin, interpreter, with_search_origin


def _intent(**overrides: object) -> SearchIntent:
    values: dict[str, object] = {
        "start_utc": datetime(2026, 8, 1, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 2, tzinfo=UTC),
        "city": "Barcelona",
        "radius_km": 2.0,
    }
    values.update(overrides)
    return SearchIntent(**values)  # type: ignore[arg-type]


def _event(name: str, latitude: float | None, longitude: float | None) -> Event:
    return Event(
        source="catalog",
        source_url=f"https://example.test/{name}",
        title=name,
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        category="tech",
        city="Barcelona",
        confidence=0.9,
        geo_lat=latitude,
        geo_lng=longitude,
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(-90.0, -180.0), (90.0, 180.0), (41.3874, 2.1686)],
)
def test_search_origin_accepts_finite_coordinates_in_range(
    latitude: float, longitude: float
) -> None:
    assert SearchOrigin(latitude=latitude, longitude=longitude).latitude == latitude


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(90.1, 0.0), (0.0, 180.1), (float("nan"), 0.0), (0.0, float("inf"))],
)
def test_search_origin_rejects_invalid_coordinates(latitude: float, longitude: float) -> None:
    with pytest.raises(ValidationError):
        SearchOrigin(latitude=latitude, longitude=longitude)


def test_with_search_origin_returns_a_copy_without_mutating_intent() -> None:
    intent = _intent()
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)

    attached = with_search_origin(intent, origin)

    assert intent.origin is None
    assert attached.origin == origin
    assert attached is not intent


@pytest.mark.parametrize("radius", [-0.1, float("nan"), float("inf")])
def test_search_intent_rejects_invalid_radius(radius: float) -> None:
    with pytest.raises(ValidationError):
        _intent(radius_km=radius)


def test_interpreter_contract_has_no_coordinate_surface() -> None:
    reflected = signature(interpreter._record_search_intent)
    # ADR 0020: the interpreter now exposes two tool schemas (search + chat).
    # Neither may leak coordinate surface.
    public_text = "\n".join(
        (
            interpreter._record_search_intent.__doc__ or "",
            interpreter._reply_chat.__doc__ or "",
            interpreter._SYSTEM_PROMPT,
            str(interpreter.SEARCH_TOOL_SCHEMA),
            str(interpreter.CHAT_TOOL_SCHEMA),
            getsource(interpreter._fallback_search_route),
        )
    ).lower()

    assert {"origin", "latitude", "longitude"}.isdisjoint(reflected.parameters)
    assert "latitude" not in public_text
    assert "longitude" not in public_text
    assert "origin" not in public_text


def test_radius_filter_is_inclusive_ordered_and_excludes_coordinate_less_events() -> None:
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    at_origin = _event("first", 41.3874, 2.1686)
    boundary_latitude = 41.405386432
    boundary = _event("boundary", boundary_latitude, 2.1686)
    absent = _event("absent", None, None)
    outside = _event("outside", 41.42, 2.1686)
    radius = _haversine_km(origin.latitude, origin.longitude, boundary_latitude, 2.1686)

    filtered = filter_events_for_intent(
        (at_origin, boundary, absent, outside), _intent(radius_km=radius, origin=origin)
    )

    assert filtered.error_type is None
    assert [event.title for event in filtered.events] == ["first", "boundary"]


def test_radius_filter_passes_events_through_unchanged_without_a_radius() -> None:
    events = (_event("coordinate-less", None, None), _event("located", 41.3, 2.1))

    filtered = filter_events_for_intent(events, _intent(radius_km=None))

    assert filtered.error_type is None
    assert filtered.events == events


def test_radius_filter_fails_closed_without_an_application_owned_origin() -> None:
    filtered = filter_events_for_intent((_event("located", 41.3, 2.1),), _intent())

    assert filtered.error_type == "missing_search_origin"
    assert filtered.events == ()


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(41.3, None), (None, 2.1), (float("nan"), 2.1)],
)
def test_event_rejects_partial_or_non_finite_coordinates(
    latitude: float | None, longitude: float | None
) -> None:
    with pytest.raises(ValidationError):
        _event("invalid", latitude, longitude)


def test_save_event_default_coordinates_are_stored_as_unknown(tmp_path, monkeypatch) -> None:
    from planazo.storage import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    result = save_event(
        title="Unknown location",
        category="tech",
        source="catalog",
        source_url="https://example.test/unknown",
        start_utc="2026-08-01T00:00:00+00:00",
        end_utc="2026-08-02T00:00:00+00:00",
        city="Barcelona",
        confidence=0.9,
    )

    saved = result["saved"]
    assert isinstance(saved, dict)
    assert saved["geo_lat"] is None
    assert saved["geo_lng"] is None
