"""Deterministic radius filtering for already validated catalog events."""

from __future__ import annotations

from collections.abc import Sequence
from math import asin, cos, radians, sin, sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict

from planazo.catalog.models import Event
from planazo.query.models import SearchIntent

_EARTH_RADIUS_KM = 6_371.0088


class RadiusFilterResult(BaseModel):
    """Ordered radius-filter result, including its fail-closed error branch."""

    model_config = ConfigDict(frozen=True)

    events: tuple[Event, ...] = ()
    error_type: Literal["missing_search_origin"] | None = None


def _haversine_km(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    """Return the deterministic great-circle distance between two points in kilometres."""
    latitude_delta = radians(latitude_b - latitude_a)
    longitude_delta = radians(longitude_b - longitude_a)
    latitude_a_radians = radians(latitude_a)
    latitude_b_radians = radians(latitude_b)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_a_radians) * cos(latitude_b_radians) * sin(longitude_delta / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(haversine))


def filter_events_for_intent(events: Sequence[Event], intent: SearchIntent) -> RadiusFilterResult:
    """Apply ``intent``'s radius without changing the caller's event ordering."""
    if intent.radius_km is None:
        return RadiusFilterResult(events=tuple(events))
    if intent.origin is None:
        return RadiusFilterResult(error_type="missing_search_origin")

    origin = intent.origin
    matching = tuple(
        event
        for event in events
        if event.geo_lat is not None
        and event.geo_lng is not None
        and _haversine_km(origin.latitude, origin.longitude, event.geo_lat, event.geo_lng)
        <= intent.radius_km
    )
    return RadiusFilterResult(events=matching)
