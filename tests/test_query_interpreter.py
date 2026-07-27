import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest

from agentlib.core import CHEAP, Result
from planazo.agents import event_agent
from planazo.agents.loop import LoopResult
from planazo.memory import facts, rules
from planazo.query import interpret
from planazo.query import interpreter as query_interpreter
from planazo.schemas.events import EventCategory, SearchIntent
from planazo.storage import db


def _tool_call_result(tool_calls: list[dict[str, Any]]) -> Result:
    output_items = [
        {
            "type": "function_call",
            "name": tc["name"],
            "arguments": "{}",
            "call_id": tc.get("call_id", "call_1"),
        }
        for tc in tool_calls
    ]
    return Result(
        text="",
        model=CHEAP,
        status="completed",
        stop_reason=None,
        truncated=False,
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=0,
        cost_usd=0.0,
        reasoning_summary=None,
        tool_calls=tool_calls,
        output_items=output_items,
    )


FROZEN_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze `_now` so the fallback window is deterministic in-test."""
    monkeypatch.setattr(query_interpreter, "_now", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture
def isolated_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the rules dir, the docstore, and the domain store at a test tree.

    Mirrors the fixture in `test_event_agent.py` — `run_once(user_id=...)`
    reads all three, so leaving any at its default would touch committed
    rules or create `var/` files.
    """
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    return rules_dir


def _assert_fallback(intent: SearchIntent, frozen: datetime) -> None:
    assert intent.error_type == "interpreter_fallback"
    assert intent.city == "Barcelona"
    assert intent.categories == ()
    assert intent.radius_km is None
    assert intent.budget_cents is None
    assert intent.start_utc == frozen
    assert intent.end_utc == frozen + timedelta(hours=72)


# --------------------------------------------------------------------------
# Happy paths + fallback branches (LLM mocked at the module import).
# --------------------------------------------------------------------------


def test_interpret_returns_a_populated_intent_on_a_well_formed_tool_call(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T18:00:00+00:00",
            "end_utc": "2026-08-01T23:00:00+00:00",
            "city": "Barcelona",
            "categories": "tech,networking",
            "radius_km": 2.0,
            "budget_cents": 1500,
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("find me tech and networking events tonight")

    assert intent.error_type is None
    assert intent.city == "Barcelona"
    assert intent.categories == ("tech", "networking")
    assert intent.radius_km == 2.0
    assert intent.budget_cents == 1500
    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)
    assert intent.start_utc.tzinfo == UTC
    assert intent.end_utc.tzinfo == UTC


def test_negative_sentinels_land_as_none_on_the_intent(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    # Sentinel semantics: radius_km/budget_cents at their defaults on the
    # wire (or explicitly negative) become `None` on the model. `0` stays
    # `0` because free events / point-radius are legitimate values.
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T18:00:00+00:00",
            "end_utc": "2026-08-01T23:00:00+00:00",
            "city": "Barcelona",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("anything this week")

    assert intent.error_type is None
    assert intent.radius_km is None
    assert intent.budget_cents is None


def test_unknown_category_in_arguments_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T18:00:00+00:00",
            "end_utc": "2026-08-01T23:00:00+00:00",
            "city": "Barcelona",
            "categories": "crypto",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("crypto meetups")

    _assert_fallback(intent, frozen_now)


def test_end_before_start_in_arguments_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T23:00:00+00:00",
            "end_utc": "2026-08-01T18:00:00+00:00",
            "city": "Barcelona",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("something later today")

    _assert_fallback(intent, frozen_now)


def test_non_iso_datetime_in_arguments_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "tomorrow",
            "end_utc": "2026-08-01T23:00:00+00:00",
            "city": "Barcelona",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("tomorrow please")

    _assert_fallback(intent, frozen_now)


def test_empty_tool_calls_from_the_model_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    monkeypatch.setattr(query_interpreter, "call", MagicMock(return_value=_tool_call_result([])))

    intent = interpret("no tool call this turn")

    _assert_fallback(intent, frozen_now)


def test_wrong_tool_name_from_the_model_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "something_else",
        "arguments": {"whatever": "value"},
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("does not matter")

    _assert_fallback(intent, frozen_now)


def test_llm_exception_falls_back_without_propagating(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    def raise_provider_down(**_kwargs: Any) -> Result:
        raise RuntimeError("provider down")

    monkeypatch.setattr(query_interpreter, "call", raise_provider_down)

    intent = interpret("provider is down")

    _assert_fallback(intent, frozen_now)


def test_empty_query_still_invokes_the_model_and_never_short_circuits(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    # The issue's "never silently defaults" clause: an empty text still
    # asks the LLM. The fallback is reached through the same branch as any
    # other failure — here, the mocked reply carries no tool call.
    mock_call = MagicMock(return_value=_tool_call_result([]))
    monkeypatch.setattr(query_interpreter, "call", mock_call)

    intent = interpret("")

    assert mock_call.call_count == 1
    assert mock_call.call_args.kwargs["prompt"] == ""
    _assert_fallback(intent, frozen_now)


# --------------------------------------------------------------------------
# Structural contracts on the module surface.
# --------------------------------------------------------------------------


def test_tool_schema_is_derived_via_schema_for_not_hand_rolled() -> None:
    source_path = Path(query_interpreter.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # (a) The schema-donor function is defined exactly once.
    donor_defs = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_record_search_intent"
    )
    assert donor_defs == 1

    # (b) Every top-level `TOOL_SCHEMA = ...` assignment is a
    # `schema_for(...)` call — not a dict literal, not a dict-comp, not a
    # variable holding a hand-rolled dict. Locks the contract structurally
    # so a future refactor cannot silently drift to a hand-written schema.
    tool_schema_assignments: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL_SCHEMA":
                    tool_schema_assignments.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TOOL_SCHEMA"
            and node.value is not None
        ):
            tool_schema_assignments.append(node.value)

    assert tool_schema_assignments, "TOOL_SCHEMA is never assigned"
    for value in tool_schema_assignments:
        assert isinstance(value, ast.Call), value
        assert isinstance(value.func, ast.Name), value.func
        assert value.func.id == "schema_for"

    # Runtime fingerprint alongside the AST check: derived shape matches
    # the plan's wire convention (CSV categories, three required args).
    assert query_interpreter.TOOL_SCHEMA["name"] == "_record_search_intent"
    assert query_interpreter.TOOL_SCHEMA["parameters"]["properties"]["categories"] == {
        "type": "string"
    }
    assert query_interpreter.TOOL_SCHEMA["parameters"]["required"] == [
        "start_utc",
        "end_utc",
        "city",
    ]


def test_no_source_module_outside_planazo_query_imports_the_interpreter() -> None:
    # The plan's tree-grep contract: `rg -l "from planazo.query" src`
    # and `rg -l "planazo.query.interpreter" src` return no path
    # outside the query package. Both patterns are the actual reach
    # channels — a mention of `planazo.query` in prose (e.g. `events.py`'s
    # docstring pointing back here) is not a consumer.
    query_dir = Path(query_interpreter.__file__).resolve().parent
    src_root = query_dir.parent.parent  # src/
    offenders: list[tuple[Path, str]] = []
    for py in src_root.rglob("*.py"):
        if query_dir in py.parents or py == query_dir:
            continue
        text = py.read_text(encoding="utf-8")
        for pattern in ("from planazo.query", "planazo.query.interpreter"):
            if pattern in text:
                offenders.append((py, pattern))
    assert offenders == [], f"interpreter is imported outside its own module: {offenders}"


def test_run_once_never_composes_the_interpreter_into_the_agent_registry(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive check on the composed registry — not just module scope: a
    # future refactor that re-composed the registry from the query module
    # would fail this. Widest tool-set combination (user_id + calendar).
    mock_run_loop = MagicMock(return_value=LoopResult(answer="ok", steps=1, stopped="answered"))
    monkeypatch.setattr(event_agent, "run_loop", mock_run_loop)

    event_agent.run_once("hi", user_id=1, calendar_enabled=True)

    registry = mock_run_loop.call_args.kwargs["registry"]
    assert "interpret" not in registry
    assert "_record_search_intent" not in registry


def test_interpret_docstring_binds_callers_to_branch_on_error_type() -> None:
    # Rule-4 seam: the fallback is structurally indistinguishable from a
    # happy-path intent, so the docstring is the contract M6's /find
    # handler PR inherits — not tribal knowledge.
    doc = interpret.__doc__ or ""
    assert "Callers MUST check result.error_type" in doc
    assert "Barcelona" in doc
    assert "interpreter_fallback" in doc


def test_every_event_category_value_appears_in_doc_schema_and_prompt() -> None:
    categories = get_args(EventCategory)
    donor_doc = query_interpreter._record_search_intent.__doc__ or ""
    schema_description = query_interpreter.TOOL_SCHEMA["description"]
    prompt = query_interpreter._SYSTEM_PROMPT

    assert categories, "EventCategory has no members — the test is checking nothing"
    for category in categories:
        assert category in donor_doc, category
        assert category in schema_description, category
        assert category in prompt, category


def test_mixed_aware_and_naive_iso_arguments_reach_the_happy_path(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    # A well-formed but mixed-tz LLM reply must not degrade into the
    # fallback branch. SearchIntent's mode="before" validators normalize
    # both endpoints; this test locks the interpreter against a silent
    # regression to a TypeError-on-compare.
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T18:00:00+00:00",
            "end_utc": "2026-08-01T23:00:00",
            "city": "Barcelona",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = interpret("find me events tonight")

    assert intent.error_type is None
    assert intent.start_utc.tzinfo == UTC
    assert intent.end_utc.tzinfo == UTC
    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)
