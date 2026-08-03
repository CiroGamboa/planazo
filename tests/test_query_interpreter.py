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
from planazo.query.models import ChatRoute, EventCategory, SearchIntent, SearchRoute
from planazo.storage import db


def _search_intent(text: str) -> SearchIntent:
    """Call `interpret` and unwrap to a `SearchIntent`, asserting the search branch.

    Every legacy test in this file was written when `interpret` returned
    a `SearchIntent` directly. ADR 0020 changed the return to a
    `RoutedMessage` discriminated union. This helper unwraps for the
    tests that expect the search branch, and it fails loudly if a test's
    mock happens to produce a chat route so a regression in the mock
    setup is visible.
    """
    routed = interpret(text)
    assert isinstance(routed, SearchRoute), f"expected search route, got {routed!r}"
    return routed.intent


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
    assert intent.end_utc == frozen + timedelta(days=30)


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

    intent = _search_intent("find me tech and networking events tonight")

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

    intent = _search_intent("anything this week")

    assert intent.error_type is None
    assert intent.radius_km is None
    assert intent.budget_cents is None


def test_record_search_intent_limit_sentinel_round_trips() -> None:
    # `_record_search_intent`'s own sentinel contract, called directly rather
    # than through a mocked `interpret()` turn — mirrors the negative-sentinel
    # discipline already proven above for `radius_km` / `budget_cents`.
    unset = query_interpreter._record_search_intent(
        start_utc="2026-08-01T18:00:00+00:00",
        end_utc="2026-08-01T23:00:00+00:00",
        city="Barcelona",
        limit=-1,
    )
    assert unset.limit is None

    five = query_interpreter._record_search_intent(
        start_utc="2026-08-01T18:00:00+00:00",
        end_utc="2026-08-01T23:00:00+00:00",
        city="Barcelona",
        limit=5,
    )
    assert five.limit == 5


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

    intent = _search_intent("crypto meetups")

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

    intent = _search_intent("something later today")

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

    intent = _search_intent("tomorrow please")

    _assert_fallback(intent, frozen_now)


def test_empty_tool_calls_from_the_model_falls_back(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    monkeypatch.setattr(query_interpreter, "call", MagicMock(return_value=_tool_call_result([])))

    intent = _search_intent("no tool call this turn")

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

    intent = _search_intent("does not matter")

    _assert_fallback(intent, frozen_now)


def test_llm_exception_falls_back_without_propagating(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    def raise_provider_down(**_kwargs: Any) -> Result:
        raise RuntimeError("provider down")

    monkeypatch.setattr(query_interpreter, "call", raise_provider_down)

    intent = _search_intent("provider is down")

    _assert_fallback(intent, frozen_now)


def test_empty_query_still_invokes_the_model_and_never_short_circuits(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    # The issue's "never silently defaults" clause: an empty text still
    # asks the LLM. The fallback is reached through the same branch as any
    # other failure — here, the mocked reply carries no tool call.
    mock_call = MagicMock(return_value=_tool_call_result([]))
    monkeypatch.setattr(query_interpreter, "call", mock_call)

    intent = _search_intent("")

    assert mock_call.call_count == 1
    assert mock_call.call_args.kwargs["prompt"] == ""
    _assert_fallback(intent, frozen_now)


# --------------------------------------------------------------------------
# Structural contracts on the module surface.
# --------------------------------------------------------------------------


def test_tool_schemas_are_derived_via_schema_for_not_hand_rolled() -> None:
    """ADR 0020: the router registers two tools, both schema_for-derived.

    Structural guard on both `SEARCH_TOOL_SCHEMA` and `CHAT_TOOL_SCHEMA`
    — a future refactor that hand-rolls either schema fails this AST
    check, matching the pre-router lock.
    """
    source_path = Path(query_interpreter.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # (a) Both schema-donor functions defined exactly once.
    donor_defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_record_search_intent", "_reply_chat"}
    }
    assert donor_defs == {"_record_search_intent", "_reply_chat"}

    # (b) Every top-level `*_TOOL_SCHEMA = ...` assignment is a
    # `schema_for(...)` call. Locks against a drift to hand-written dicts.
    tool_schema_assignments: list[ast.expr] = []
    schema_names = {"SEARCH_TOOL_SCHEMA", "CHAT_TOOL_SCHEMA"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in schema_names:
                    tool_schema_assignments.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in schema_names
            and node.value is not None
        ):
            tool_schema_assignments.append(node.value)

    assert len(tool_schema_assignments) == 2, (
        f"expected both SEARCH_TOOL_SCHEMA + CHAT_TOOL_SCHEMA; got {len(tool_schema_assignments)}"
    )
    for value in tool_schema_assignments:
        assert isinstance(value, ast.Call), value
        assert isinstance(value.func, ast.Name), value.func
        assert value.func.id == "schema_for"

    # Runtime fingerprint on the search schema — locks the CSV convention.
    assert query_interpreter.SEARCH_TOOL_SCHEMA["name"] == "_record_search_intent"
    assert query_interpreter.SEARCH_TOOL_SCHEMA["parameters"]["properties"]["categories"] == {
        "type": "string"
    }
    assert query_interpreter.SEARCH_TOOL_SCHEMA["parameters"]["required"] == [
        "start_utc",
        "end_utc",
        "city",
    ]
    # Chat schema: single required `text` arg (1..500 chars — the caller
    # of the schema doesn't enforce the range; that's the model boundary).
    assert query_interpreter.CHAT_TOOL_SCHEMA["name"] == "_reply_chat"
    assert query_interpreter.CHAT_TOOL_SCHEMA["parameters"]["required"] == ["text"]


def test_only_the_cli_surface_imports_the_interpreter_outside_planazo_query() -> None:
    # The invariant this locks is tighter than "no `from planazo.query`":
    # `planazo.query.models` is a *data* module (SearchIntent, EventCategory)
    # that other bounded contexts legitimately import — the Recommender for
    # its intent argument, the calendar boundary for the shared category
    # literal. What must never be imported outside `planazo.query/` is the
    # *runtime* — `interpret`, `_record_search_intent`, `TOOL_SCHEMA`. Those
    # only ever reach the tree through `planazo.query.interpreter` (or
    # `planazo.query import interpret` from the package `__init__`).
    #
    # Two legitimate importers today: `agents/cli.py` (the terminal
    # surface's `/find` REPL) and `conversation/service.py` (the
    # multi-turn `/find` composition root the bot's `handle_find`
    # calls — see ADR 0016). Both are surfaces above the Recommender
    # that own the interpreter's one call per user turn.
    query_dir = Path(query_interpreter.__file__).resolve().parent
    src_root = query_dir.parent.parent  # src/
    offenders: list[tuple[Path, str]] = []
    for py in src_root.rglob("*.py"):
        if query_dir in py.parents or py == query_dir:
            continue
        text = py.read_text(encoding="utf-8")
        for pattern in ("planazo.query.interpreter", "from planazo.query import"):
            if pattern in text:
                offenders.append((py, pattern))
    agents_dir = Path(event_agent.__file__).parent
    conversation_dir = agents_dir.parent / "conversation"
    # `Path.rglob` gives no ordering guarantee, so compare as a set rather
    # than an exact list — the invariant is which files import the
    # runtime, not the filesystem's traversal order.
    assert set(offenders) == {
        (agents_dir / "cli.py", "planazo.query.interpreter"),
        (conversation_dir / "service.py", "planazo.query.interpreter"),
    }, f"unexpected interpreter-runtime import: {offenders}"


def test_run_once_never_composes_the_interpreter_into_the_agent_registry(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive check on the composed registry — not just module scope: a
    # future refactor that re-composed the registry from the query module
    # would fail this. Widest tool-set combination (user_id + calendar).
    mock_run_loop = MagicMock(return_value=LoopResult(answer="ok", steps=1, stopped="answered"))
    monkeypatch.setattr(event_agent, "_run_recommender_graph", mock_run_loop)

    event_agent.run_once(
        1,
        SearchIntent(
            start_utc=FROZEN_NOW,
            end_utc=FROZEN_NOW + timedelta(hours=1),
            city="Barcelona",
        ),
        calendar_enabled=True,
    )

    registry = mock_run_loop.call_args.kwargs["registry"]
    assert "interpret" not in registry
    assert "_record_search_intent" not in registry


def test_interpret_docstring_binds_callers_to_branch_on_kind_and_error_type() -> None:
    """The docstring is the router's contract — ADR 0020's discriminator lives here."""
    doc = interpret.__doc__ or ""
    # Callers must branch on `.kind` per ADR 0020.
    assert "kind" in doc
    # Fallback still lives on the search branch — ADR 0020 §D3.
    assert "Barcelona" in doc
    assert "interpreter_fallback" in doc


def test_every_event_category_value_appears_in_doc_schema_and_prompt() -> None:
    categories = get_args(EventCategory)
    donor_doc = query_interpreter._record_search_intent.__doc__ or ""
    schema_description = query_interpreter.SEARCH_TOOL_SCHEMA["description"]
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

    intent = _search_intent("find me events tonight")

    assert intent.error_type is None
    assert intent.start_utc.tzinfo == UTC
    assert intent.end_utc.tzinfo == UTC
    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)


# --------------------------------------------------------------------------
# ADR 0020 router branch: chat + fallback-is-never-chat.
# --------------------------------------------------------------------------


def test_interpret_returns_chat_route_on_reply_chat_tool_call(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    """LLM classifies text as small-talk → `ChatRoute` with the reply verbatim."""
    tool_call = {
        "name": "_reply_chat",
        "arguments": {"text": "¡Hola! Estoy aquí para recomendarte eventos en Barcelona."},
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    routed = interpret("Hola")

    assert isinstance(routed, ChatRoute)
    assert routed.kind == "chat"
    assert routed.answer == "¡Hola! Estoy aquí para recomendarte eventos en Barcelona."


def test_chat_route_answer_is_bounded_at_the_pydantic_boundary() -> None:
    """ADR 0020: the LLM's chat reply is capped 1..500 chars.

    An over-long reply from the model triggers the fallback branch —
    `interpret` never propagates a Pydantic error to the caller.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChatRoute(answer="x" * 501)
    with pytest.raises(ValidationError):
        ChatRoute(answer="")


def test_interpret_fallback_never_lands_as_chat_route(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    """ADR 0020 §D3: an over-long chat reply degrades to search fallback, not a fake chat.

    Locks the invariant that the display layer's uncertainty signal
    (`error_type="interpreter_fallback"` on the search branch) cannot
    be silently masked by a chat route with degraded text.
    """
    tool_call = {
        "name": "_reply_chat",
        "arguments": {"text": "x" * 1000},  # over 500-char cap → ChatRoute raises
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    routed = interpret("Hi")

    assert isinstance(routed, SearchRoute), f"expected fallback SearchRoute, got {routed!r}"
    _assert_fallback(routed.intent, frozen_now)


def test_interpret_prompt_documents_both_tools() -> None:
    """The system prompt names both tools so the model knows when each fires."""
    prompt = query_interpreter._SYSTEM_PROMPT
    assert "_record_search_intent" in prompt
    assert "_reply_chat" in prompt
    # Explicit disambiguation guidance.
    assert "greeting" in prompt.lower()
    assert "meta-question" in prompt.lower()


def test_interpret_search_only_unwraps_a_chat_route_as_fallback(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    """`interpret_search_only` is the clarification-path escape hatch.

    ADR 0020 §D5: the clarification-answer path is a more specific
    state than the router. When a user mid-clarification sends a
    numeric-only "2" or a bare "music", the LLM might legitimately
    classify it as small-talk; the clarification path bypasses the
    router by calling `interpret_search_only`, which returns a
    well-formed SearchIntent (falling back to the 30d default) so
    `run_once` can process the answer.
    """
    tool_call = {
        "name": "_reply_chat",
        "arguments": {"text": "That's a nice number!"},  # LLM misroutes "2" as chat
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = query_interpreter.interpret_search_only("2")

    assert isinstance(intent, SearchIntent)
    _assert_fallback(intent, frozen_now)


def test_interpret_search_only_returns_the_search_intent_when_router_agrees(
    monkeypatch: pytest.MonkeyPatch, frozen_now: datetime
) -> None:
    tool_call = {
        "name": "_record_search_intent",
        "arguments": {
            "start_utc": "2026-08-01T18:00:00+00:00",
            "end_utc": "2026-08-01T23:00:00+00:00",
            "city": "Barcelona",
            "categories": "music",
        },
        "call_id": "call_1",
    }
    monkeypatch.setattr(
        query_interpreter, "call", MagicMock(return_value=_tool_call_result([tool_call]))
    )

    intent = query_interpreter.interpret_search_only("music")

    assert intent.error_type is None
    assert intent.categories == ("music",)
