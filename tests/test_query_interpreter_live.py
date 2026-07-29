"""Opt-in live interpreter test — real LLM parses `/find` free text.

Marked `live`, deselected by default (`pyproject.toml [tool.pytest.ini_options]
addopts = "-m 'not live'"`). Run explicitly:

    uv run pytest -m live tests/test_query_interpreter_live.py -v -s

Requires a real `OPENCODE_API_KEY`. Three calls to the CHEAP model, no other
tool use — well under the repo's per-suite cost ceiling for a live file.

Proves the issue #110 behaviour end-to-end against the real interpreter
system prompt: a user-stated count parses into `SearchIntent.limit`, and a
query naming no time window at all gets the widened 30-day fallback (not the
old, dataset-clipping 72h).
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from dotenv import find_dotenv, load_dotenv

from planazo.query import interpret


def _real_key_present() -> bool:
    key = os.environ.get("OPENCODE_API_KEY")
    return bool(key) and key != "test-key-not-real"


@pytest.fixture(autouse=True)
def _load_real_env() -> None:
    """Load the real `.env` only when a live test actually runs.

    Mirrors `test_agents_gate_live.py`: `tests/conftest.py` sets a placeholder
    `OPENCODE_API_KEY` via `setdefault` for the mocked suite, and doing the
    override at import time would clobber that placeholder even for
    `pytest -m 'not live'` runs. Scoping the override to test execution keeps
    the mocked suite's safety net intact.
    """
    load_dotenv(find_dotenv(), override=True)
    if not _real_key_present():
        pytest.skip("OPENCODE_API_KEY not set to a real value")


@pytest.mark.live
def test_interpret_parses_a_user_stated_count_into_limit() -> None:
    intent = interpret("give me 3 music events this weekend")

    # Assert on `.limit` first — that's the load-bearing new behavior this
    # test exists to prove.
    assert intent.limit == 3
    assert intent.error_type is None
    assert "music" in intent.categories


@pytest.mark.live
def test_interpret_parses_top_n_phrasing_into_limit() -> None:
    intent = interpret("top 5 tech events")

    assert intent.limit == 5
    assert intent.error_type is None


@pytest.mark.live
def test_interpret_with_no_stated_count_leaves_limit_unset_and_widens_the_window() -> None:
    intent = interpret("music events")

    assert intent.limit is None
    assert intent.error_type is None
    # Proves the 72h -> 30d fallback widening: a query naming no time window
    # gets a window of at least 20 days, not the old 72-hour default.
    assert intent.end_utc - intent.start_utc >= timedelta(days=20)
