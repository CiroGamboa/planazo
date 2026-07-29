"""Opt-in live end-to-end test — a real model actually uses the memory tools.

Marked `live`, deselected by default (same convention as
`tests/test_agents_gate_live.py` / `tests/test_recommender_limit_live.py`).
Run explicitly:

    uv run pytest -m live tests/agents/test_memory_resurfaces_live.py -v -s

Requires a real `OPENCODE_API_KEY`.

This is the test that proves `data/rules/010-memory-usage.md`, pushed with
no code change via `memory.rules.load_rules()`, is enough to make a real
model call `save_memory` and `retrieve_memory` on its own initiative, not
just a scripted mock. The mocked-plumbing side of the same claim lives in
`tests/agents/test_memory_resurfaces.py`.

`event_agent.run_once` now accepts a `text` run_context key — the user's raw
message this turn, pushed as bounded context alongside the validated intent
(`docs/adr/0022-user-text-push-context.md`). This test drives that channel
directly with an explicit remember-ask, which the merged rule allows
`save_memory` to act on in a single turn.

Known limitation, not covered here: the rule's other trigger — "the same
preference has been implied twice this conversation" — needs at least one
earlier turn's raw wording to still be visible on a later turn.
`run_once`'s `text` key only ever carries the *current* turn's message; no
cross-turn buffer exists yet, so that clause is not exercised by this test.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dotenv import find_dotenv, load_dotenv

from agentlib.core import CHEAP
from planazo.agents.event_agent import RecommenderResult, run_once
from planazo.agents.loop import StepRecord
from planazo.memory import facts
from planazo.query.models import SearchIntent
from planazo.storage import db

pytestmark = pytest.mark.live

_MAX_ATTEMPTS = 3
_REMEMBER_ASK_TEXT = "Please remember that I don't like loud, crowded venues."


def _real_key_present() -> bool:
    key = os.environ.get("OPENCODE_API_KEY")
    return bool(key) and key != "test-key-not-real"


@pytest.fixture(autouse=True)
def _load_real_env() -> None:
    """Load the real `.env` only when a live test actually runs.

    Same discipline as `test_agents_gate_live.py` / `test_recommender_limit_live.py`
    — `tests/conftest.py`'s placeholder key must survive `pytest -m 'not live'`.
    """
    load_dotenv(find_dotenv(), override=True)
    if not _real_key_present():
        pytest.skip("OPENCODE_API_KEY not set to a real value")


@pytest.fixture
def isolated_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """Point every store at a fresh tmp tree; leave `rules.RULES_DIR` at its
    committed default so the real `data/rules/*.md` (including
    `010-memory-usage.md`) is what actually reaches the model.

    Mirrors `test_recommender_limit_live.py`'s `isolated_catalog` fixture.
    Returns the seeded user's id.
    """
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")

    conn = db.connect()
    try:
        from planazo.identity import get_or_create_user

        user = get_or_create_user(conn, "tg-live-memory-1", "Live Memory Test User")
        assert user.id is not None
        return user.id
    finally:
        conn.close()


def _intent(**overrides: object) -> SearchIntent:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "start_utc": now + timedelta(days=1),
        "end_utc": now + timedelta(days=8),
        "city": "Barcelona",
    }
    values.update(overrides)
    return SearchIntent(**values)  # type: ignore[arg-type]


def _run_until_save_memory_is_called(
    user_id: int, tmp_path: Path
) -> tuple[RecommenderResult, list[StepRecord]]:
    """Run the live Recommender until it actually calls `save_memory`.

    Retries the *observation* only, exactly like
    `test_agents_gate_live.py::_run_until_the_model_requests_the_gated_tool`:
    whether the model acts on the new rules file on a given attempt is model
    behaviour, not a property of this code, so only the observation is
    retried — the assertions about what happened once it did act run once,
    on the attempt that produced the call.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        steps: list[StepRecord] = []
        result = run_once(
            user_id,
            _intent(),
            model=CHEAP,
            max_steps=6,
            max_output_tokens=400,
            on_step=steps.append,
            run_log_dir=tmp_path / f"turn1-{attempt}",
            text=_REMEMBER_ASK_TEXT,
        )
        if any(step.tool == "save_memory" for step in steps):
            print(f"\n[live memory] save_memory called on attempt {attempt}/{_MAX_ATTEMPTS}")
            return result, steps

    pytest.fail(
        f"model never called save_memory in {_MAX_ATTEMPTS} attempts — a model-behaviour "
        "observation, not necessarily a defect here; check whether data/rules/010-memory-"
        "usage.md's wording or the text push is what needs tightening."
    )


@pytest.mark.live
def test_a_real_model_saves_and_later_resurfaces_a_durable_preference(
    isolated_stores: int, tmp_path: Path
) -> None:
    user_id = isolated_stores

    # --- Turn 1: an explicit remember-ask in the raw message; per
    # data/rules/010-memory-usage.md, the model should call save_memory.
    turn_1_result, turn_1_steps = _run_until_save_memory_is_called(user_id, tmp_path)
    save_call = next(step for step in turn_1_steps if step.tool == "save_memory")
    print(f"\n[live memory] save_memory args: {save_call.arguments}")
    print(f"[live memory] turn 1 answer: {turn_1_result.answer!r}")

    saved_content = save_call.arguments.get("content", "")
    assert isinstance(saved_content, str) and saved_content

    # --- Turn 2 (later, unrelated intent): the model should retrieve the
    # fact on its own — this is what "resurfaces without being told" means.
    turn_2_steps: list[StepRecord] = []
    turn_2_result = run_once(
        user_id,
        _intent(),
        model=CHEAP,
        max_steps=6,
        max_output_tokens=400,
        on_step=turn_2_steps.append,
        run_log_dir=tmp_path / "turn2",
    )
    print(f"\n[live memory] turn 2 answer: {turn_2_result.answer!r}")

    retrieve_calls = [step for step in turn_2_steps if step.tool == "retrieve_memory"]
    assert retrieve_calls, "turn 2 never called retrieve_memory — the fact cannot have resurfaced"

    # Mechanical proof the saved fact was actually handed back on the later
    # turn (the retrieved payload, not the model's own paraphrase of it).
    returned_facts = [fact for call in retrieve_calls for fact in call.result["facts"]]
    assert any(fact["content"] == saved_content for fact in returned_facts), (
        f"retrieve_memory never returned the fact saved in turn 1 ({saved_content!r}); "
        f"got {returned_facts!r}"
    )

    # Soft proof the model's own prose reflects it — recorded for human review
    # regardless (`-v -s`), asserted loosely because prose wording is not
    # this ticket's contract.
    assert turn_2_result.answer, "turn 2 produced no final answer to inspect"
