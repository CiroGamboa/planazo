from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from planazo.agents import cli, event_agent
from planazo.agents.event_agent import RecommenderResult
from planazo.interfaces.runtime import RecommenderResult as RuntimeRecommenderResult
from planazo.monitor.models import RunStep
from planazo.query import SearchIntent


def _radius_intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        radius_km=2.0,
    )


def test_missing_origin_stops_before_any_composition_or_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = MagicMock(side_effect=AssertionError("preflight must stop first"))
    monkeypatch.setattr(event_agent, "load_rules", blocked)
    monkeypatch.setattr(event_agent, "_read_preferences", blocked)
    monkeypatch.setattr(event_agent, "run_loop", blocked)
    monkeypatch.setattr(event_agent, "RunStepLogger", blocked)

    result = event_agent.run_once(
        1,
        _radius_intent(),
        run_id="missing-origin",
        run_log_dir=tmp_path,
    )

    assert result == RecommenderResult(
        status="error",
        answer=event_agent.MISSING_SEARCH_ORIGIN_ANSWER,
        error_type="missing_search_origin",
        steps=0,
        stopped="not_started",
    )
    blocked.assert_not_called()
    assert not (tmp_path / "missing-origin.jsonl").exists()


def test_missing_origin_is_a_runtime_and_cli_safe_error() -> None:
    runtime = RuntimeRecommenderResult(
        status="error", stopped="not_started", steps=0, error_type="missing_search_origin"
    )
    rendered = cli._render_result(
        RecommenderResult(
            status="error", stopped="not_started", steps=0, error_type="missing_search_origin"
        )
    )

    assert runtime.stopped == "not_started"
    assert runtime.error_type == "missing_search_origin"
    assert "trusted search origin" in rendered


@pytest.mark.parametrize("result_type", [RecommenderResult, RuntimeRecommenderResult])
def test_recommender_result_mirrors_reject_incompatible_outcomes(result_type: type[object]) -> None:
    with pytest.raises(ValidationError):
        result_type(  # type: ignore[operator]
            status="incomplete",
            stopped="truncated",
            steps=1,
            candidates=(),
            error_type="search_not_completed",
        )


def test_monitor_trace_rejects_missing_search_origin() -> None:
    with pytest.raises(ValidationError):
        RunStep(
            run_id="run-1",
            agent="recommender",
            started_at="2026-07-27T12:00:00Z",
            recorded_at="2026-07-27T12:00:01Z",
            model="gpt-5.4-nano",
            model_tier="cheap",
            user_message="Find events",
            step=1,
            wall_clock_ms=10,
            phase="completion",
            stopped="missing_search_origin",
        )
