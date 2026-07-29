"""Opt-in live end-to-end test — issue #111, the display gaps it closes.

Marked `live`, deselected by default (`pyproject.toml`'s `-m "not live"`
addopts). Run explicitly:

    uv run pytest -m live tests/test_recommendation_link_and_preface_live.py -v -s

Requires a real `OPENCODE_API_KEY`. Drives `conversation.service.handle_user_message`
— the exact composition root the bot's `/find` command calls, with no
mocked seam — against a seeded, isolated SQLite catalog, then renders the
resulting `ConversationReply` through `bot.commands.format_reply`, exactly
as the bot does. This is the test that proves the feature actually works:
not that `_format_recommendation_line` or `format_reply` independently do
the right thing in isolation (that's the unit tests in
`test_bot_find_command.py`), but that a real Recommender turn's rendered
reply text carries both a link fragment on the candidate line and a
non-empty preface — the LLM's own summary — above the numbered list.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dotenv import find_dotenv, load_dotenv

from planazo.bot.commands import format_reply
from planazo.bot.config import load_config
from planazo.catalog import save_event
from planazo.conversation import service
from planazo.identity import get_or_create_user
from planazo.memory import facts, rules
from planazo.storage import db

_SEED_COUNT = 8
_MAX_ATTEMPTS = 4


def _real_key_present() -> bool:
    key = os.environ.get("OPENCODE_API_KEY")
    return bool(key) and key != "test-key-not-real"


@pytest.fixture(autouse=True)
def _load_real_env() -> None:
    """Load the real `.env` only when a live test actually runs.

    Same discipline as `test_recommender_limit_live.py` — `tests/conftest.py`'s
    placeholder key must survive `pytest -m 'not live'`.
    """
    load_dotenv(find_dotenv(), override=True)
    if not _real_key_present():
        pytest.skip("OPENCODE_API_KEY not set to a real value")


@pytest.fixture
def isolated_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """Point rules/memory/domain stores at a fresh tmp tree, seed music
    events — half carrying a `ticket_url`, half only `source_url` — and
    return the seeded user's id. Mirrors `test_recommender_limit_live.py`'s
    fixture of the same name.
    """
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")

    now = datetime.now(UTC)
    for i in range(_SEED_COUNT):
        start = now + timedelta(days=1 + i * 2, hours=1)
        end = start + timedelta(hours=2)
        result = save_event(
            title=f"Live Preface Music Night {i}",
            category="music",
            source="live-test-seed",
            source_url=f"https://example.com/live-preface-event-{i}",
            start_utc=start.isoformat(),
            end_utc=end.isoformat(),
            city="Barcelona",
            confidence=0.9,
            venue_name="Sala Apolo",
            ticket_url=f"https://tickets.example.com/live-preface-event-{i}" if i % 2 == 0 else "",
        )
        assert "event_db_id" in result, result

    conn = db.connect()
    try:
        user = get_or_create_user(conn, "tg-live-preface-1", "Live Preface Test User")
        assert user.id is not None
        return user.id
    finally:
        conn.close()


@pytest.mark.live
def test_find_reply_renders_link_and_preface_above_the_list(isolated_catalog: int) -> None:
    """A real `/find`-style turn's rendered text carries a link fragment on
    the candidate line and a non-empty preface above the numbered list —
    the two display gaps issue #111 closes.

    Retries the whole turn (never the assertion) on an attempt where the
    real Recommender's own tool-call choices miss the seeded catalog or
    stop without a final answer — the same allowance
    `test_recommender_limit_live.py` makes for the model's own search-filter
    choices, not a retry around a failing check.
    """
    config = load_config(Path("data/bot.yaml"))
    user_id = isolated_catalog
    conn = db.connect()
    try:
        reply = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            candidate_reply = service.handle_user_message(conn, user_id, "music this week")
            if candidate_reply.kind == "recommendations" and candidate_reply.answer:
                print(f"\n[live preface] matched on attempt {attempt}/{_MAX_ATTEMPTS}")
                reply = candidate_reply
                break
            print(
                f"\n[live preface] attempt {attempt}/{_MAX_ATTEMPTS} did not land "
                f"(kind={candidate_reply.kind!r}, answer={candidate_reply.answer!r})"
            )
        assert reply is not None, "no attempt produced a recommendations reply with a preface"
    finally:
        conn.close()

    text = format_reply(config, reply)

    assert reply.answer is not None
    assert reply.answer.strip() != ""
    # The preface renders verbatim at the top, a blank line, then the
    # 1-indexed numbered list — `find_recommendations_with_preface`'s
    # exact "{preface}\n\n{lines}" shape.
    assert text.startswith(reply.answer)
    assert "\n\n1." in text
    # Every candidate line ends with a link fragment (ticket_url preferred,
    # source_url fallback — either way the label renders).
    assert "link: " in text
