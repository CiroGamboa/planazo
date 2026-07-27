"""Live-LLM tests for the approval gate.

Opt-in only: default `pytest` runs skip these (see `addopts = "-m 'not live'"`).
Run them with `uv run pytest -m live tests/test_agents_gate_live.py -v -s`.

Both tests hit real OpenCode Zen through `agentlib`, the CHEAP model, with
`max_output_tokens=256` and `max_steps=3` — a hard cost cap. Each retries its
precondition up to 3 times, so the worst case is two tests at 3 attempts each,
≈0.5¢ per call: ≈3¢ against the ticket's documented 5¢ ceiling.

Both pass `calendar_enabled=True`: `confirm_and_create_calendar_event` is the
only irreversible tool in the tree and it is opt-in, so without that flag the
model is never offered the tool the gate exists to guard.

Whether the model *requests* the gated tool is a model-behaviour observation,
not a property of this code, and it competes for attention with every tool
schema and every line of pushed rules text the run carries. So the retry in
`_run_until_the_model_requests_the_gated_tool` covers obtaining that
observation, and prints the attempt it took; the assertions each test makes
about the gate run once, on the attempt that produced a gated call.

Environment setup: `agent/tests/conftest.py` sets `OPENCODE_API_KEY` to a
placeholder via `os.environ.setdefault(...)` for the mocked suite. The
`_load_real_env` fixture below reloads `.env` with `override=True` — but
only when a live test actually runs, so the mocked suite keeps its
placeholder safety net (module import alone would not be enough).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import find_dotenv, load_dotenv

from agentlib.core import CHEAP
from planazo.agents.event_agent import run_once
from planazo.agents.loop import LoopResult
from planazo.approval import ApprovalGate

pytestmark = pytest.mark.live

_LIVE_PROMPT = (
    "Please confirm and create the calendar event for event_id 'evt-42', "
    "with no invitees to notify."
)

_MAX_ATTEMPTS = 3


def _real_key_present() -> bool:
    key = os.environ.get("OPENCODE_API_KEY")
    return bool(key) and key != "test-key-not-real"


@pytest.fixture(autouse=True)
def _load_real_env() -> None:
    """Load the real .env only when a live test actually runs.

    Doing this at module import time would fire during pytest collection on
    every invocation — even `pytest -m 'not live'` — and overwrite the
    placeholder key that keeps the mocked suite from making accidental real
    calls. The fixture scopes the override to the live-test execution path.
    """
    load_dotenv(find_dotenv(), override=True)
    if not _real_key_present():
        pytest.skip("OPENCODE_API_KEY not set to a real value")


@pytest.fixture
def _redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    candidates_path = tmp_path / "candidates.json"
    calendar_path = tmp_path / "calendar_events.json"
    monkeypatch.setattr("tools.tools.CANDIDATES_PATH", candidates_path)
    monkeypatch.setattr("tools.tools.CALENDAR_EVENTS_PATH", calendar_path)
    # A prior turn "already saved" this candidate — confirm_... looks it up
    # by event_id rather than taking full event details fresh.
    candidates_path.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "event_id": "evt-42",
                    "title": "AI Meetup",
                    "category": "tech",
                    "source": "meetup",
                    "start_time": "2026-08-01T19:00:00",
                    "location": "Barcelona",
                    "confidence": 0.9,
                }
            ]
        ),
        encoding="utf-8",
    )
    return calendar_path


def _run_until_the_model_requests_the_gated_tool(
    approve: MagicMock, gate: ApprovalGate
) -> LoopResult:
    """Run the live prompt until the model actually requests the gated tool.

    Shared by both tests so their preconditions cannot drift apart. Retries the
    *observation* only, never an assertion: a run in which the model simply
    chose not to act says nothing about the gate, while what the gate does once
    the model acts — and whether the loop recovers from a refusal — is ours and
    is asserted exactly once, on the returned run.

    The attempt count is printed unconditionally (this file is run with `-v -s`).
    Silence would let the retry absorb decay: at a degraded request rate, three
    attempts still pass most of the time, so a real suppression regression would
    read as a flake that a re-run clears. Record the printed number.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        result = run_once(
            _LIVE_PROMPT,
            model=CHEAP,
            max_output_tokens=256,
            max_steps=3,
            gate=gate,
            calendar_enabled=True,
        )
        if approve.called:
            print(f"\n[live gate] gated tool requested on attempt {attempt}/{_MAX_ATTEMPTS}")
            return result

    print(f"\n[live gate] gated tool never requested in {_MAX_ATTEMPTS} attempts")
    pytest.fail(
        f"model did not request the gated tool in {_MAX_ATTEMPTS} attempts — this is a "
        "model-behaviour observation, not a gate defect; check whether recently added "
        "tools or rules text are suppressing it. (An assertion that fails *after* a "
        "gated call landed is the other case, and that one is a gate defect.)"
    )


def test_gate_approve_path_with_real_llm(_redirect_stores: Path) -> None:
    calendar_path = _redirect_stores
    approve = MagicMock(return_value=True)
    gate = ApprovalGate(
        tool_names=frozenset({"confirm_and_create_calendar_event"}), approve=approve
    )

    result = _run_until_the_model_requests_the_gated_tool(approve, gate)

    tool_name, args = approve.call_args.args
    assert tool_name == "confirm_and_create_calendar_event"
    assert args.get("event_id") == "evt-42"

    assert calendar_path.exists(), "approved tool call did not persist"
    entries = json.loads(calendar_path.read_text())
    # `>= 1`, not `== 1`: the file accumulates across retry attempts, and an
    # earlier attempt that dispatched before this one is not a defect.
    assert len(entries) >= 1
    assert entries[0]["event_id"] == "evt-42"

    assert result.stopped == "answered"


def test_gate_decline_path_with_real_llm(_redirect_stores: Path) -> None:
    calendar_path = _redirect_stores
    approve = MagicMock(return_value=False)
    gate = ApprovalGate(
        tool_names=frozenset({"confirm_and_create_calendar_event"}), approve=approve
    )

    result = _run_until_the_model_requests_the_gated_tool(approve, gate)

    tool_name, _ = approve.call_args.args
    assert tool_name == "confirm_and_create_calendar_event"

    if calendar_path.exists():
        entries = json.loads(calendar_path.read_text())
        assert entries == [], "declined tool call was persisted anyway"

    assert result.stopped == "answered"
