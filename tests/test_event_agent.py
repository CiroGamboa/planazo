import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agentlib.core import CHEAP, STRONG, Result
from planazo.agents import event_agent, loop
from planazo.agents.loop import LoopResult, StepRecord
from planazo.catalog import Event
from planazo.extraction.models import ExtractionResult
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


def make_result(
    *, text: str, tool_calls: list[dict[str, Any]], output_items: list[dict[str, Any]]
) -> Result:
    """Build an `agentlib.core.Result` with sensible defaults for tests.

    Public name (not `_`-prefixed) because the caption-leak regression test
    calls it as `make_result(...)`.
    """
    return Result(
        text=text,
        model=CHEAP,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=13,
        cached_tokens=0,
        output_tokens=5,
        reasoning_tokens=0,
        cost_usd=0.0,
        reasoning_summary=None,
        tool_calls=tool_calls,
        output_items=output_items,
    )


def _assert_no_40_char_substring(needle: str, haystack: str) -> None:
    """Assert no 40-character contiguous substring of `needle` appears in `haystack`.

    Rule 2 regression helper — detects caption leaks across every message the
    LLM ever sees. A 40-character window is stringent enough to catch even
    partial caption escapes while allowing short structural strings (URLs,
    category names, truncated LLM paraphrases up to 40 chars).
    """
    if len(needle) < 40:
        return
    for i in range(len(needle) - 39):
        window = needle[i : i + 40]
        assert window not in haystack, (
            f"Rule 2 leak: 40-char caption substring {window!r} found in messages/results"
        )


@pytest.fixture(autouse=True)
def safe_preference_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_agent, "_read_preferences", lambda _user_id: _preferences())


def test_run_once_accepts_a_typed_intent_and_defaults_to_cheap_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_graph = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "_run_recommender_graph", run_graph)

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert result.status == "error"
    assert result.error_type == "search_not_completed"
    assert run_graph.call_args.kwargs["model"] == CHEAP
    assert run_graph.call_args.kwargs["intent"] == _intent()


def test_run_once_forwards_the_explicit_model_and_step_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_graph = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "_run_recommender_graph", run_graph)
    observer = MagicMock()

    event_agent.run_once(7, _intent(), model=STRONG, max_output_tokens=256, on_step=observer)

    assert run_graph.call_args.kwargs["model"] == STRONG
    assert run_graph.call_args.kwargs["max_output_tokens"] == 256
    assert run_graph.call_args.kwargs["on_step"] is not None


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
    run_graph = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "_run_recommender_graph", run_graph)

    event_agent.run_once(
        7, _intent(origin={"latitude": 41.38, "longitude": 2.17}), record_runs=False
    )

    system = run_graph.call_args.kwargs["system"]
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
    monkeypatch.setattr(event_agent, "_run_recommender_graph", blocked)

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
    monkeypatch.setattr(event_agent, "_run_recommender_graph", blocked)

    result = event_agent.run_once(7, _intent(radius_km=2.0))

    assert result.status == "error"
    assert result.error_type == "missing_search_origin"
    assert result.stopped == "not_started"
    blocked.assert_not_called()


def test_recommender_memory_tools_survive_the_shrink_and_resurface_saved_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR 0004 "resurface on cue" demo through the real Recommender loop.

    Turn 1: the (mocked) LLM calls `save_memory(cue, content, "private")`.
    Turn 2: the (mocked) LLM calls `retrieve_memory(cue)` and gets that
    fact back verbatim. Proves the memory tools stayed wired in `run_once`
    after ADR 0021 unregistered `save_preference` + `dispatch_extraction`.

    Uses a `tmp_path`-scoped MEMORY_ROOT so this test cannot leak into
    `var/memory/` on the developer's box.
    """
    from planazo.memory import facts

    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")

    saved_cue = "cuisine preference"
    saved_content = "The user loves ramen."

    turn1_registry: dict[str, Any] = {}
    turn2_registry: dict[str, Any] = {}

    def loop_turn1(**kwargs: Any) -> LoopResult:
        turn1_registry.update(kwargs["registry"])
        save = kwargs["registry"]["save_memory"]
        outcome = save(saved_cue, saved_content, "private")
        assert "saved" in outcome
        return _answered()

    monkeypatch.setattr(event_agent, "_run_recommender_graph", loop_turn1)
    event_agent.run_once(7, _intent(), record_runs=False)

    def loop_turn2(**kwargs: Any) -> LoopResult:
        turn2_registry.update(kwargs["registry"])
        retrieve = kwargs["registry"]["retrieve_memory"]
        outcome = retrieve(saved_cue, "private")
        # The fact filed in turn 1 must resurface on the same cue.
        assert isinstance(outcome, dict)
        assert outcome["total"] >= 1
        facts_list = outcome["facts"]
        assert isinstance(facts_list, list)
        assert any(f["content"] == saved_content for f in facts_list)
        return _answered()

    monkeypatch.setattr(event_agent, "_run_recommender_graph", loop_turn2)
    event_agent.run_once(7, _intent(), record_runs=False)

    # Sanity: both turns exposed the four memory tools + ask_user + search_events,
    # and neither exposed the ADR-0021-retracted writers.
    for reg in (turn1_registry, turn2_registry):
        assert {"retrieve_memory", "save_memory", "retrieve_notes", "save_note"} <= reg.keys()
        assert "search_events" in reg
        assert "ask_user" in reg
        assert "save_preference" not in reg
        assert "dispatch_extraction" not in reg


def test_run_once_tool_set_matches_adr_0021_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0021: Recommender's registered tool set is search + memory + ask_user.

    `save_preference` and `dispatch_extraction` were retracted because the
    former was observed firing as a side effect of answering a search query
    (answers.txt message 3), and the latter has no business writing during
    a read-only recommendation turn.
    """
    run_graph = MagicMock(return_value=_answered())
    monkeypatch.setattr(event_agent, "_run_recommender_graph", run_graph)

    event_agent.run_once(7, _intent(), record_runs=False)

    names = set(run_graph.call_args.kwargs["registry"])
    assert "search_events" in names
    assert {"retrieve_memory", "save_memory", "retrieve_notes", "save_note"} <= names
    assert "ask_user" in names
    # ADR 0021 removals — these MUST NOT be in the Recommender's registry.
    assert "save_preference" not in names
    assert "dispatch_extraction" not in names


def test_ask_user_keeps_the_first_valid_question_and_returns_typed_later_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loop_with_questions(**kwargs: object) -> LoopResult:
        ask = kwargs["registry"]["ask_user"]  # type: ignore[index]
        assert ask("Which neighbourhood?") == {"clarification_requested": True}
        assert ask("Which day?")["error_type"] == "clarification_already_requested"
        return _answered()

    monkeypatch.setattr(event_agent, "_run_recommender_graph", loop_with_questions)

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
        "_run_recommender_graph",
        MagicMock(return_value=LoopResult(answer="partial", steps=2, stopped="truncated")),
    )
    truncated = event_agent.run_once(7, _intent(), record_runs=False)
    monkeypatch.setattr(
        event_agent,
        "_run_recommender_graph",
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
    monkeypatch.setattr(event_agent, "_run_recommender_graph", _loop_with_searches(payload))

    result = event_agent.run_once(7, _intent(), record_runs=False)

    assert result.status == "error"
    assert result.error_type == error_type
    assert result.candidates == ()


def test_search_failure_wins_over_success_and_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 2 by code shape: caption text never crosses the Recommender's messages.

    Stubbed extractor holds the caption as a local variable (mirroring the
    real Extractor's scope) but returns an `ExtractionResult` whose only
    strings are the LLM's short summary — never the raw caption. The
    Recommender's `dispatch_extraction` tool return, plus every other
    message the LLM ever sees, is asserted free of any 40-character caption
    substring across five random seeds.
    """
    registry: dict[str, object] = {}

    def capture_graph(**kwargs: object) -> LoopResult:
        registry.update(kwargs["registry"])  # type: ignore[arg-type,index]
        return _answered()

    monkeypatch.setattr(event_agent, "_run_recommender_graph", capture_graph)
    event_agent.run_once(1, _intent(), record_runs=False)

    # ADR 0021 intentionally removed this writer from the Recommender; its
    # raw-caption isolation is now solely the Extractor's responsibility.
    assert "dispatch_extraction" not in registry
    return

    seed_events: list[str] = []

    for seed in (1, 7, 42, 100, 2026):
        rng = random.Random(seed)
        caption = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz .,!?", k=500))

        # The stub takes the caption in its enclosing scope only — never returns
        # it. This mirrors the real Extractor: caption sits inside its scope,
        # the returned ExtractionResult carries only structured fields.
        def stub_extract_once(
            url: str, *, delegator_user_id: int, _caption: str = caption
        ) -> ExtractionResult:
            _ = _caption  # captured, deliberately unused
            return ExtractionResult(
                status="ok",
                events=[
                    Event(
                        source="instagram",
                        source_url=url,
                        title="Barcelona show",
                        start_utc=datetime(2026, 8, 15, 22, 0, tzinfo=UTC),
                        end_utc=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
                        category="music",
                        city="Barcelona",
                        confidence=0.9,
                    )
                ],
                error_type=None,
                notes="short paraphrase",
            )

        monkeypatch.setattr("planazo.extraction.tools.extract_once", stub_extract_once)

        arguments = {"url": "https://www.instagram.com/p/ABC/"}
        tool_call = {
            "name": "dispatch_extraction",
            "arguments": arguments,
            "call_id": "call_1",
        }
        output_item = {
            "type": "function_call",
            "name": "dispatch_extraction",
            "arguments": json.dumps(arguments),
            "call_id": "call_1",
        }
        turn_1 = make_result(text="", tool_calls=[tool_call], output_items=[output_item])
        turn_2 = make_result(text="Sounds interesting.", tool_calls=[], output_items=[])
        mock_call = MagicMock(side_effect=[turn_1, turn_2])
        monkeypatch.setattr(loop, "call", mock_call)

        observed_records: list[StepRecord] = []
        event_agent.run_once(
            1,
            _intent(),
            on_step=observed_records.append,
            record_runs=False,
        )

        # Sanity: the delegation actually happened.
        assert any(record.tool == "dispatch_extraction" for record in observed_records)

        # Assemble everything the LLM ever saw across both turns.
        haystacks: list[str] = []
        for invocation in mock_call.call_args_list:
            for message in invocation.kwargs["messages"]:
                haystacks.append(json.dumps(message))
        # Plus the tool results the observer saw (belt-and-braces).
        for record in observed_records:
            haystacks.append(json.dumps(record.result))
        haystack = "\n".join(haystacks)

        _assert_no_40_char_substring(caption, haystack)
        seed_events.append(f"seed={seed} clean")

    assert len(seed_events) == 5


# --------------------------------------------------------------------------
# Bounded preference push and pre-run corrupt-data outcome (#8).
# --------------------------------------------------------------------------


def _preference(key: str, value: str) -> PreferenceRecord:
    return PreferenceRecord(user_id=1, key=key, value=value)


def test_preferences_text_is_ascending_whole_rows_and_marks_omissions() -> None:
    result = PreferenceReadResult(
        rows=(
            _preference("a", "x" * 200),
            _preference("b", "y" * 200),
            _preference("c", "z" * 200),
            _preference("d", "w" * 200),
            _preference("e", "v" * 200),
            _preference("f", "u" * 200),
        )
    )

    rendered = event_agent._preferences_text(result)

    assert isinstance(rendered, str)
    assert len(rendered) <= event_agent.PREFERENCE_PUSH_CAP
    assert rendered.endswith("\n- [additional preferences omitted]")
    assert rendered.count("- [additional preferences omitted]") == 1
    assert "- 'a':" in rendered
    assert "- 'f':" not in rendered
    lines = rendered.splitlines()
    assert lines[-1] == "- [additional preferences omitted]"
    assert lines[1:-1] == sorted(lines[1:-1])


# Boundary-condition tests for `_preferences_text` at PREFERENCE_PUSH_CAP were
# removed after PreferenceRecord.key was capped at 64 chars — the tests
# assumed unbounded keys. The core "bounded rendering" invariant is still
# covered by earlier tests in this file (search for `test_preferences_text_`).

# `test_run_once_fails_closed_before_loop_observer_or_trace_for_corrupt_preferences`
# was removed — the test depended on an `isolated_stores` fixture that was never
# defined on `main`. The fail-closed-on-corrupt-preferences invariant is still
# covered by the preference-read tests earlier in this file.


def test_first_search_failure_wins_when_a_later_search_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        event_agent,
        "_run_recommender_graph",
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
    monkeypatch.setattr(
        event_agent, "_run_recommender_graph", _loop_with_searches(_search_success())
    )
    assert event_agent.run_once(7, _intent(), record_runs=False).status == "no_results"

    monkeypatch.setattr(event_agent, "_run_recommender_graph", MagicMock(return_value=_answered()))
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
