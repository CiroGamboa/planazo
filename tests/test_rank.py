from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from planazo.agents.event_agent import ClarificationRequest, RecommenderResult
from planazo.catalog.models import Event
from planazo.query.models import SearchIntent, SearchOrigin
from planazo.rank import RankedEvent, RankingPreferences, rank_events


def _intent(*, radius_km: float | None = None, origin: SearchOrigin | None = None) -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 31, tzinfo=UTC),
        city="Barcelona",
        radius_km=radius_km,
        origin=origin,
    )


def _event(
    *,
    title: str = "Event",
    category: str = "music",
    starts_in_days: int = 1,
    confidence: float = 0.5,
    source_url: str = "https://example.test/event",
    geo_lat: float | None = None,
    geo_lng: float | None = None,
) -> Event:
    start = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=starts_in_days)
    return Event(
        source="test",
        source_url=source_url,
        title=title,
        start_utc=start,
        end_utc=start + timedelta(hours=2),
        category=category,
        city="Barcelona",
        geo_lat=geo_lat,
        geo_lng=geo_lng,
        confidence=confidence,
    )


@pytest.mark.parametrize(
    "value",
    [
        "music",
        b"music",
        {"music": True},
        (item for item in ["music"]),
        [""],
        ["x\ny"],
        ["x" * 65],
    ],
)
def test_ranking_preferences_rejects_non_contract_values(value: object) -> None:
    with pytest.raises(ValidationError):
        RankingPreferences(preferred_categories=value)


def test_ranking_preferences_normalizes_and_deduplicates() -> None:
    preferences = RankingPreferences(preferred_categories=[" Music ", "music", "TECH"])
    assert preferences.preferred_categories == ("music", "tech")


def test_ranking_preferences_rejects_more_than_twenty_values() -> None:
    with pytest.raises(ValidationError):
        RankingPreferences(preferred_categories=[f"category-{number}" for number in range(21)])


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
def test_ranked_event_rejects_invalid_score(score: float) -> None:
    with pytest.raises(ValidationError):
        RankedEvent(event=_event(), score=score, reason="A valid reason.")


@pytest.mark.parametrize("reason", ["", "line one\nline two", "x" * 241])
def test_ranked_event_rejects_invalid_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        RankedEvent(event=_event(), score=0.5, reason=reason)


def test_preference_reason_wins_equal_eligible_scores() -> None:
    event = _event(category="Music", confidence=1.0)
    result = rank_events([event], _intent(), RankingPreferences(preferred_categories=["music"]))
    assert result[0].reason == "Matches your preferred category: Music."


def test_freshness_and_confidence_reasons_are_deterministic() -> None:
    freshness = rank_events([_event(confidence=0.5)], _intent(), RankingPreferences())[0]
    confidence = rank_events(
        [_event(starts_in_days=20, confidence=1.0)], _intent(), RankingPreferences()
    )[0]
    assert freshness.reason == "Starts near the beginning of your requested time window."
    assert confidence.reason == "High source confidence for this event."


def test_neutral_result_uses_exact_fallback() -> None:
    result = rank_events(
        [_event(starts_in_days=30, confidence=0.5)], _intent(), RankingPreferences()
    )
    assert result[0].reason == "General match for your search."


def test_radius_proximity_reason_uses_derived_distance_only() -> None:
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    event = _event(geo_lat=41.3880, geo_lng=2.1690, starts_in_days=20)
    result = rank_events([event], _intent(radius_km=2.0, origin=origin), RankingPreferences())
    assert result[0].reason.startswith("Within your search radius: ")
    assert "41.3874" not in result[0].reason
    assert "2.1686" not in result[0].reason


def test_radius_requires_origin() -> None:
    with pytest.raises(ValueError, match="radius_km requires origin"):
        rank_events([_event()], _intent(radius_km=1.0), RankingPreferences())


def test_active_radius_rejects_model_copy_with_partial_coordinates() -> None:
    malformed = _event().model_copy(update={"geo_lat": 41.3874, "geo_lng": None})
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    with pytest.raises(ValueError, match="complete coordinates"):
        rank_events([malformed], _intent(radius_km=2.0, origin=origin), RankingPreferences())


def test_positive_radius_boundary_is_ranked_without_a_proximity_reason() -> None:
    from planazo.catalog import haversine_km

    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    boundary_latitude = 41.3974
    radius_km = haversine_km(origin.latitude, origin.longitude, boundary_latitude, 2.1686)
    event = _event(geo_lat=boundary_latitude, geo_lng=2.1686, starts_in_days=20)
    ranked = rank_events([event], _intent(radius_km=radius_km, origin=origin), RankingPreferences())
    assert ranked[0].reason == "General match for your search."


def test_zero_radius_only_scores_exact_origin_as_nearby() -> None:
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    exact = _event(geo_lat=41.3874, geo_lng=2.1686, starts_in_days=20)
    other = _event(geo_lat=41.3884, geo_lng=2.1686, starts_in_days=20, source_url="https://e/2")
    ranked = rank_events([other, exact], _intent(radius_km=0, origin=origin), RankingPreferences())
    assert ranked[0].event == exact


def test_coordinate_less_event_is_neutral_without_radius() -> None:
    result = rank_events([_event(starts_in_days=20)], _intent(), RankingPreferences())
    assert result[0].score == pytest.approx((1 - 20 / 30) * 0.35 + 0.5 * 0.30 + 0.5 * 0.15)


def test_coordinate_less_event_is_neutral_with_active_radius() -> None:
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    result = rank_events(
        [_event(starts_in_days=20)], _intent(radius_km=2.0, origin=origin), RankingPreferences()
    )
    assert result[0].score == pytest.approx((1 - 20 / 30) * 0.35 + 0.5 * 0.30 + 0.5 * 0.15)
    assert result[0].reason == "General match for your search."


def test_long_valid_category_renders_a_bounded_single_line_reason() -> None:
    category = "m" * 64
    event = _event(category=category, confidence=0.5)
    ranked = rank_events([event], _intent(), RankingPreferences(preferred_categories=[category]))
    assert len(ranked[0].reason) <= 240
    assert "\n" not in ranked[0].reason
    assert ranked[0].reason.endswith(".")


def test_tie_breaking_uses_start_title_url_then_input_position() -> None:
    first = _event(title="B", starts_in_days=20, source_url="https://e/b")
    second = _event(title="A", starts_in_days=20, source_url="https://e/z")
    third = _event(title="A", starts_in_days=20, source_url="https://e/a")
    assert [
        item.event for item in rank_events([first, second, third], _intent(), RankingPreferences())
    ] == [
        third,
        second,
        first,
    ]


def test_final_tie_breaker_preserves_original_position() -> None:
    first = _event(title="Same", starts_in_days=20, source_url="https://e/same")
    second = _event(title="Same", starts_in_days=20, source_url="https://e/same")
    ranked = rank_events([first, second], _intent(), RankingPreferences())
    assert ranked[0].event is first
    assert ranked[1].event is second


@pytest.mark.parametrize(
    ("event", "preferences", "expected_reason"),
    [
        (
            _event(category="music", geo_lat=41.3874, geo_lng=2.1686, confidence=1.0),
            RankingPreferences(preferred_categories=["music"]),
            "Matches your preferred category: music.",
        ),
        (
            _event(geo_lat=41.3874, geo_lng=2.1686, confidence=1.0),
            RankingPreferences(),
            "Within your search radius: 0.0 km away.",
        ),
        (
            _event(starts_in_days=0, confidence=1.0),
            RankingPreferences(),
            "Starts near the beginning of your requested time window.",
        ),
        (
            _event(starts_in_days=20, confidence=1.0),
            RankingPreferences(),
            "High source confidence for this event.",
        ),
    ],
)
def test_reason_priority_breaks_complete_component_ties(
    event: Event, preferences: RankingPreferences, expected_reason: str
) -> None:
    origin = SearchOrigin(latitude=41.3874, longitude=2.1686)
    ranked = rank_events([event], _intent(radius_km=2.0, origin=origin), preferences)
    assert ranked[0].reason == expected_reason


def test_naive_candidate_start_is_rejected() -> None:
    event = _event().model_copy(update={"start_utc": datetime(2026, 8, 2)})
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_events([event], _intent(), RankingPreferences())


def test_ranking_modules_do_not_depend_on_agent_or_storage_boundaries() -> None:
    import ast
    from pathlib import Path

    forbidden = ("agentlib", "planazo.agents", "storage", "memory", "interpreter", "bot")
    root = Path(__file__).parents[1] / "src" / "planazo" / "rank"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name.startswith(forbidden) for name in imported)


def _rank_successful_recommender_result(
    result: RecommenderResult, intent: SearchIntent, preferences: RankingPreferences
) -> list[RankedEvent]:
    """Local contract-only example for a future result consumer."""
    if result.status != "ok":
        return []
    return rank_events(result.candidates, intent, preferences)


@pytest.mark.parametrize(
    "result",
    [
        RecommenderResult(status="no_results", stopped="answered", steps=1),
        RecommenderResult(
            status="needs_clarification",
            stopped="answered",
            steps=1,
            clarification=ClarificationRequest(question="Which day?"),
        ),
        RecommenderResult(status="incomplete", stopped="max_steps", steps=8),
        RecommenderResult(
            status="error", stopped="answered", steps=1, error_type="search_not_completed"
        ),
    ],
)
def test_local_result_status_guard_never_calls_ranker_for_non_ok(
    monkeypatch: pytest.MonkeyPatch, result: RecommenderResult
) -> None:
    calls: list[tuple[Event, ...]] = []

    def spy(
        candidates: tuple[Event, ...], _intent: SearchIntent, _preferences: RankingPreferences
    ) -> list[RankedEvent]:
        calls.append(candidates)
        return []

    monkeypatch.setattr(sys.modules[__name__], "rank_events", spy)
    assert _rank_successful_recommender_result(result, _intent(), RankingPreferences()) == []
    assert calls == []


def test_local_result_status_guard_calls_ranker_with_exact_ok_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = (_event(), _event(source_url="https://example.test/event-two"))
    result = RecommenderResult(status="ok", stopped="answered", steps=1, candidates=candidates)
    received: tuple[Event, ...] | None = None

    def spy(
        value: tuple[Event, ...], _intent: SearchIntent, _preferences: RankingPreferences
    ) -> list[RankedEvent]:
        nonlocal received
        received = value
        return []

    monkeypatch.setattr(sys.modules[__name__], "rank_events", spy)
    assert _rank_successful_recommender_result(result, _intent(), RankingPreferences()) == []
    assert received is result.candidates
