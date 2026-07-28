from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from planazo.agents import cli, event_agent
from planazo.agents.loop import LoopResult
from planazo.interfaces.runtime import LoopResult as RuntimeLoopResult
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
        "events near me",
        intent=_radius_intent(),
        user_id=1,
        run_id="missing-origin",
        run_log_dir=tmp_path,
    )

    assert result == LoopResult(
        answer=event_agent.MISSING_SEARCH_ORIGIN_ANSWER,
        steps=0,
        stopped="missing_search_origin",
    )
    blocked.assert_not_called()
    assert not (tmp_path / "missing-origin.jsonl").exists()


def test_missing_origin_is_a_runtime_and_cli_safe_error() -> None:
    runtime = RuntimeLoopResult(answer=None, steps=0, stopped="missing_search_origin")
    rendered = cli._render_result(LoopResult(answer=None, steps=0, stopped="missing_search_origin"))

    assert runtime.stopped == "missing_search_origin"
    assert "trusted search origin" in rendered


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
