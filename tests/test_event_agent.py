import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agentlib.core import CHEAP, STRONG
from planazo.agents import event_agent
from planazo.agents.loop import LoopResult, StepRecord
from planazo.catalog import Event
from planazo.identity import PreferenceReadResult, PreferenceRecord
from planazo.memory import rules
from planazo.query.models import SearchIntent


def _intent(**overrides: object) -> SearchIntent:
    values: dict[str, object] = {
        "start_utc": datetime(2026, 8, 1, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 2, tzinfo=UTC),
        "city": "Barcelona",
        "categories": ("tech",),
    }
    values.update(overrides)
    return SearchIntent(**values)  # type: ignore[arg-type]


def _preferences(*rows: PreferenceRecord) -> PreferenceReadResult:
    return PreferenceReadResult(rows=rows)


def _answered() -> LoopResult:
    return LoopResult(answer="done", steps=1, stopped="answered")


def _event(**overrides: object) -> Event:
    values: dict[str, object] = {
        "id": 1,
        "source": "meetup",
        "source_url": "https://events.example/1",
        "title": "Meetup",
        "start_utc": datetime(2026, 8, 1, 18, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 20, tzinfo=UTC),
        "category": "tech",
        "city": "Barcelona",
        "price_cents": 0,
        "confidence": 0.8,
    }
    values.update(overrides)
    return Event(**values)  # type: ignore[arg-type]


def _search_success(*events: Event) -> dict[str, object]:
    return {"events": [event.model_dump(mode="json") for event in events], "total": len(events)}


def _loop_with_searches(*results: object, stopped: str = "answered"):
    def run(**kwargs: object) -> LoopResult:
        observer = kwargs["on_step"]
        for number, result in enumerate(results, start=1):
            observer(StepRecord(step=number, tool="search_events", arguments={}, result=result))  # type: ignore[operator]
        return LoopResult(answer="done", steps=max(1, len(results)), stopped=stopped)  # type: ignore[arg-type]

    return run


@pytest.fixture(autouse=True)
def safe_preference_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_agent, "_read_preferences", lambda _user_id: _preferences())


def test_run_once_accepts_a_typed_intent_and_defaults_to_cheap_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert result.status == "error"
    assert result.error_type == "search_not_completed"
    assert run_loop.call_args.kwargs["model"] == CHEAP
    assert run_loop.call_args.kwargs["user_message"] == event_agent.RECOMMENDER_WORK_MESSAGE


def test_run_once_forwards_the_explicit_model_and_step_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)
    observer = MagicMock()

    event_agent.run_once(7, _intent(), model=STRONG, max_output_tokens=256, on_step=observer)

    assert run_loop.call_args.kwargs["model"] == STRONG
    assert run_loop.call_args.kwargs["max_output_tokens"] == 256
    assert run_loop.call_args.kwargs["on_step"] is not None


def test_run_once_pushes_rules_bounded_preferences_and_intent_without_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "000-rules.md").write_text("RULES", encoding="utf-8")
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(
        event_agent,
        "_read_preferences",
        lambda _user_id: _preferences(PreferenceRecord(user_id=7, key="city", value="Barcelona")),
    )
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)

    event_agent.run_once(
        7, _intent(origin={"latitude": 41.38, "longitude": 2.17}), record_runs=False
    )

    system = run_loop.call_args.kwargs["system"]
    assert "RULES" in system
    assert "- 'city': 'Barcelona'" in system
    assert "Validated search intent" in system
    assert "origin" not in system


def test_preference_rendering_is_bounded_ordered_and_marks_omissions() -> None:
    rows = tuple(
        PreferenceRecord(user_id=7, key=f"key-{index:02}", value="x" * 200) for index in range(10)
    )

    rendered = event_agent._preferences_text(_preferences(*rows))

    assert isinstance(rendered, str)
    assert len(rendered) <= event_agent.PREFERENCE_PUSH_CAP
    assert rendered.endswith(event_agent.PREFERENCE_OMISSION_MARKER)
    assert rendered.index("key-00") < rendered.index("key-01")
    assert all(line.startswith("- ") for line in rendered.splitlines()[1:])


@pytest.mark.parametrize("error_type", ["invalid_preference_data", "preference_store_unavailable"])
def test_preference_failures_stop_before_rules_trace_or_llm(
    monkeypatch: pytest.MonkeyPatch, error_type: str
) -> None:
    blocked = MagicMock(side_effect=AssertionError("preflight must stop first"))
    monkeypatch.setattr(
        event_agent,
        "_read_preferences",
        lambda _user_id: PreferenceReadResult(error_type=error_type, message="bad row"),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(event_agent, "load_rules", blocked)
    monkeypatch.setattr(event_agent, "RunStepLogger", blocked)
    monkeypatch.setattr(event_agent, "run_loop", blocked)

    result = event_agent.run_once(7, _intent())

    assert result.status == "error"
    assert result.error_type == error_type
    assert result.stopped == "not_started"
    assert result.steps == 0
    blocked.assert_not_called()


def test_radius_without_trusted_origin_wins_over_corrupt_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = MagicMock(side_effect=AssertionError("origin check must stop first"))
    monkeypatch.setattr(event_agent, "_read_preferences", blocked)
    monkeypatch.setattr(event_agent, "load_rules", blocked)
    monkeypatch.setattr(event_agent, "RunStepLogger", blocked)
    monkeypatch.setattr(event_agent, "run_loop", blocked)

    result = event_agent.run_once(7, _intent(radius_km=2.0))

    assert result.status == "error"
    assert result.error_type == "missing_search_origin"
    assert result.stopped == "not_started"
    blocked.assert_not_called()


def test_run_once_registers_search_memory_and_real_extraction_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)

    event_agent.run_once(7, _intent(), record_runs=False)

    names = set(run_loop.call_args.kwargs["registry"])
    assert "search_events" in names
    assert "dispatch_extraction" in names
    assert {"retrieve_memory", "save_memory", "retrieve_notes", "save_note"} <= names
    assert {"save_preference", "ask_user"} <= names


def test_save_preference_is_bound_validates_and_normalizes_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)

    event_agent.run_once(7, _intent(), record_runs=False)
    save = run_loop.call_args.kwargs["registry"]["save_preference"]

    invalid = save("city\nSYSTEM", "Barcelona")
    assert invalid["error_type"] == "invalid_preference"

    stored: list[tuple[int, str, str]] = []
    monkeypatch.setattr(event_agent.db, "connect", lambda: MagicMock())
    monkeypatch.setattr(
        event_agent,
        "set_preference",
        lambda _conn, owner, key, value: (
            stored.append((owner, key, value))
            or PreferenceRecord(user_id=owner, key=key, value=value)
        ),
    )
    monkeypatch.setattr(event_agent, "get_preferences", lambda _conn, _owner: _preferences())

    saved = save(" city ", " Barcelona ")

    assert stored == [(7, "city", "Barcelona")]
    assert saved["saved"] == {"user_id": 7, "key": "city", "value": "Barcelona", "updated_at": None}


def test_save_preference_maps_identity_and_store_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)
    event_agent.run_once(7, _intent(), record_runs=False)
    save = run_loop.call_args.kwargs["registry"]["save_preference"]
    monkeypatch.setattr(event_agent.db, "connect", lambda: MagicMock())
    monkeypatch.setattr(
        event_agent, "set_preference", MagicMock(side_effect=sqlite3.IntegrityError)
    )

    assert save("city", "Barcelona")["error_type"] == "unknown_user"

    monkeypatch.setattr(event_agent.db, "connect", MagicMock(side_effect=OSError("offline")))
    assert save("city", "Barcelona")["error_type"] == "preference_store_unavailable"


def test_save_preference_fails_closed_when_reread_data_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_loop = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "run_loop", run_loop)
    event_agent.run_once(7, _intent(), record_runs=False)
    save = run_loop.call_args.kwargs["registry"]["save_preference"]
    monkeypatch.setattr(event_agent.db, "connect", lambda: MagicMock())
    monkeypatch.setattr(
        event_agent,
        "set_preference",
        lambda _conn, owner, key, value: PreferenceRecord(user_id=owner, key=key, value=value),
    )
    monkeypatch.setattr(
        event_agent,
        "get_preferences",
        lambda _conn, _owner: PreferenceReadResult(
            error_type="invalid_preference_data", message="bad persisted row"
        ),
    )

    assert save("city", "Barcelona")["error_type"] == "invalid_preference_data"


def test_ask_user_keeps_the_first_valid_question_and_returns_typed_later_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loop_with_questions(**kwargs: object) -> LoopResult:
        ask = kwargs["registry"]["ask_user"]  # type: ignore[index]
        assert ask("Which neighbourhood?") == {"clarification_requested": True}
        assert ask("Which day?")["error_type"] == "clarification_already_requested"
        return _answered()

    monkeypatch.setattr(event_agent, "run_loop", loop_with_questions)

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert result.status == "needs_clarification"
    assert result.clarification is not None
    assert result.clarification.question == "Which neighbourhood?"
    assert result.candidates == ()


def test_truncated_and_max_step_runs_are_incomplete_without_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        event_agent,
        "run_loop",
        MagicMock(return_value=LoopResult(answer="partial", steps=2, stopped="truncated")),
    )
    truncated = event_agent.run_once(7, _intent(), record_runs=False)
    monkeypatch.setattr(
        event_agent,
        "run_loop",
        MagicMock(return_value=LoopResult(answer=None, steps=8, stopped="max_steps")),
    )
    exhausted = event_agent.run_once(7, _intent(), record_runs=False)

    assert (truncated.status, truncated.stopped, truncated.candidates) == (
        "incomplete",
        "truncated",
        (),
    )
    assert (exhausted.status, exhausted.stopped, exhausted.candidates) == (
        "incomplete",
        "max_steps",
        (),
    )


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"tool_failed": True, "error": "offline"}, "search_tool_failure"),
        (
            {"error_type": "search_store_unavailable", "message": "OperationalError"},
            "search_store_unavailable",
        ),
        ({"error_type": "invalid_search_filter", "message": "bad"}, "search_invalid_filter"),
        ({"error_type": "invalid_event_data", "message": "bad"}, "invalid_search_output"),
        ({"events": [], "total": True}, "invalid_search_output"),
        ({"events": [], "total": -1}, "invalid_search_output"),
        ({"events": [], "total": 1}, "invalid_search_output"),
        ({"events": "not-a-list", "total": 0}, "invalid_search_output"),
        ({"events": [], "total": 0, "extra": "no"}, "invalid_search_output"),
        ({"events": []}, "invalid_search_output"),
        ({"events": [{"title": "missing fields"}], "total": 1}, "invalid_search_output"),
    ],
)
def test_search_observation_maps_strict_envelopes(
    monkeypatch: pytest.MonkeyPatch, payload: object, error_type: str
) -> None:
    monkeypatch.setattr(event_agent, "run_loop", _loop_with_searches(payload))

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert result.status == "error"
    assert result.error_type == error_type
    assert result.candidates == ()


def test_search_failure_wins_over_success_and_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loop(**kwargs: object) -> LoopResult:
        observer = kwargs["on_step"]
        ask = kwargs["registry"]["ask_user"]  # type: ignore[index]
        ask("Which day?")
        observer(
            StepRecord(step=1, tool="search_events", arguments={}, result=_search_success(_event()))
        )  # type: ignore[operator]
        observer(
            StepRecord(
                step=2,
                tool="search_events",
                arguments={},
                result={"tool_failed": True, "error": "bad"},
            )
        )  # type: ignore[operator]
        return _answered()

    monkeypatch.setattr(event_agent, "run_loop", loop)

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert (result.status, result.error_type, result.candidates, result.clarification) == (
        "error",
        "search_tool_failure",
        (),
        None,
    )


def test_first_search_failure_wins_when_a_later_search_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        event_agent,
        "run_loop",
        _loop_with_searches(
            {"error_type": "invalid_search_filter", "message": "bad"}, _search_success(_event())
        ),
    )

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert (result.status, result.error_type, result.candidates) == (
        "error",
        "search_invalid_filter",
        (),
    )


def test_valid_empty_search_is_no_results_and_answer_without_search_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_agent, "run_loop", _loop_with_searches(_search_success()))
    assert event_agent.run_once(7, _intent(), record_runs=False).status == "no_results"

    monkeypatch.setattr(event_agent, "run_loop", MagicMock(return_value=_answered()))
    result = event_agent.run_once(7, _intent(), record_runs=False)
    assert (result.status, result.error_type) == ("error", "search_not_completed")


def test_candidate_filtering_is_deterministic_and_applies_all_boundaries() -> None:
    retained = _event(id=1, source_url="https://events.example/kept", geo_lat=41.38, geo_lng=2.17)
    candidates = event_agent._filter_candidates(
        (
            retained,
            _event(id=1, source_url="https://events.example/id-duplicate"),
            _event(id=2, source_url="https://events.example/kept"),
            _event(id=3, category="music"),
            _event(id=4, city=" Madrid "),
            _event(id=5, start_utc=datetime(2026, 8, 2, 1, tzinfo=UTC)),
            _event(id=6, price_cents=101),
            _event(id=7, geo_lat=None, geo_lng=None),
        ),
        _intent(origin={"latitude": 41.38, "longitude": 2.17}, radius_km=1.0, budget_cents=100),
    )

    assert candidates == (retained,)


def test_rejected_duplicate_does_not_suppress_later_matching_event() -> None:
    rejected = _event(id=1, source_url="https://events.example/same", category="music")
    matching = _event(id=1, source_url="https://events.example/same", category="tech")

    candidates = event_agent._filter_candidates((rejected, matching), _intent(categories=("tech",)))

    assert candidates == (matching,)


@pytest.mark.parametrize(
    "values",
    [
        {"status": "ok", "stopped": "answered", "steps": 1},
        {
            "status": "no_results",
            "stopped": "answered",
            "steps": 1,
            "candidates": (_event(),),
        },
        {
            "status": "incomplete",
            "stopped": "truncated",
            "steps": 1,
            "clarification": {"question": "Which day?"},
        },
        {
            "status": "error",
            "stopped": "not_started",
            "steps": 0,
            "error_type": "search_not_completed",
        },
        {
            "status": "error",
            "stopped": "answered",
            "steps": 0,
            "error_type": "search_not_completed",
        },
        {
            "status": "needs_clarification",
            "stopped": "max_steps",
            "steps": 1,
            "clarification": {"question": "Which day?"},
        },
        {
            "status": "error",
            "stopped": "answered",
            "steps": 1,
            "error_type": "search_not_completed",
            "interpreter_fallback": True,
        },
    ],
)
def test_recommender_result_rejects_incompatible_outcome_fields(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        event_agent.RecommenderResult(**values)  # type: ignore[arg-type]
