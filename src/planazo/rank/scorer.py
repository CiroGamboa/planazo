"""Pure, repeatable ranking of already validated recommender candidates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from math import isfinite
from unicodedata import normalize

from planazo.catalog import haversine_km
from planazo.catalog.models import Event
from planazo.query.models import SearchIntent
from planazo.rank.models import MAX_REASON_CHARS, RankedEvent, RankingPreferences

FRESHNESS_WEIGHT = 0.35
PROXIMITY_WEIGHT = 0.30
PREFERENCE_WEIGHT = 0.20
CONFIDENCE_WEIGHT = 0.15
FRESHNESS_HORIZON_SECONDS = 30 * 24 * 60 * 60
NEUTRAL_COMPONENT_SCORE = 0.5
_SAFE_CATEGORY_CHARS = 64


def _clamp(value: float) -> float:
    if not isfinite(value):
        raise ValueError("ranking component must be finite")
    return min(1.0, max(0.0, value))


def _normalized_key(value: str) -> str:
    return normalize("NFKC", value).casefold().strip()


def _safe_category(category: str) -> str:
    return " ".join(category.split())[:_SAFE_CATEGORY_CHARS]


def _reason(
    *,
    event: Event,
    freshness: float,
    proximity: float,
    preference: float,
    confidence: float,
    distance_km: float | None,
) -> str:
    choices: list[tuple[float, int, str]] = []
    if preference == 1.0:
        choices.append(
            (preference, 0, f"Matches your preferred category: {_safe_category(event.category)}.")
        )
    if distance_km is not None and proximity > NEUTRAL_COMPONENT_SCORE:
        choices.append((proximity, 1, f"Within your search radius: {distance_km:.1f} km away."))
    if freshness > NEUTRAL_COMPONENT_SCORE:
        choices.append((freshness, 2, "Starts near the beginning of your requested time window."))
    if confidence > NEUTRAL_COMPONENT_SCORE:
        choices.append((confidence, 3, "High source confidence for this event."))
    rendered = (
        max(choices, key=lambda item: (item[0], -item[1]))[2]
        if choices
        else "General match for your search."
    )
    if "\n" in rendered or "\r" in rendered or len(rendered) > MAX_REASON_CHARS:
        raise ValueError("ranking reason is not safely renderable")
    return rendered


def _ensure_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candidate start_utc must be timezone-aware")


def rank_events(
    candidates: Sequence[Event], intent: SearchIntent, preferences: RankingPreferences
) -> list[RankedEvent]:
    """Rank candidates deterministically after a successful Recommender result."""
    if intent.radius_km is not None and intent.origin is None:
        raise ValueError("radius_km requires origin")
    ranked: list[tuple[RankedEvent, int]] = []
    for position, event in enumerate(candidates):
        _ensure_aware(event.start_utc)
        distance_km: float | None = None
        if intent.radius_km is not None:
            if (event.geo_lat is None) != (event.geo_lng is None):
                raise ValueError("active-radius candidates require complete coordinates")
            if event.geo_lat is not None and event.geo_lng is not None:
                assert intent.origin is not None
                distance_km = haversine_km(
                    intent.origin.latitude, intent.origin.longitude, event.geo_lat, event.geo_lng
                )
                proximity = (
                    1.0
                    if intent.radius_km == 0 and distance_km == 0
                    else 0.0
                    if intent.radius_km == 0
                    else 1.0 - _clamp(distance_km / intent.radius_km)
                )
            else:
                proximity = NEUTRAL_COMPONENT_SCORE
        else:
            proximity = NEUTRAL_COMPONENT_SCORE
        offset = (event.start_utc - intent.start_utc).total_seconds()
        freshness = 1.0 - _clamp(max(0.0, offset) / FRESHNESS_HORIZON_SECONDS)
        preference = (
            1.0 if event.category.strip().casefold() in preferences.preferred_categories else 0.0
        )
        confidence = _clamp(event.confidence)
        score = _clamp(
            freshness * FRESHNESS_WEIGHT
            + proximity * PROXIMITY_WEIGHT
            + preference * PREFERENCE_WEIGHT
            + confidence * CONFIDENCE_WEIGHT
        )
        ranked.append(
            (
                RankedEvent(
                    event=event,
                    score=score,
                    reason=_reason(
                        event=event,
                        freshness=freshness,
                        proximity=proximity,
                        preference=preference,
                        confidence=confidence,
                        distance_km=distance_km,
                    ),
                ),
                position,
            )
        )
    ranked.sort(
        key=lambda item: (
            -item[0].score,
            item[0].event.start_utc,
            _normalized_key(item[0].event.title),
            _normalized_key(item[0].event.source_url),
            item[1],
        )
    )
    return [item[0] for item in ranked]
