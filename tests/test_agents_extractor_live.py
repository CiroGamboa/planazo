"""Opt-in live extraction test — one real Instagram post through the real LLM.

Marked `live`, deselected by default (`pyproject.toml [tool.pytest.ini_options]
addopts = "-m 'not live'"`). Run explicitly:

    uv run pytest -m live tests/test_agents_extractor_live.py -v -s

Requires:

- `OPENCODE_API_KEY` (real credential)
- Network access to `api.opencode.com` and to Instagram's CDN

The target is one hardcoded static-post URL from a well-known public
Barcelona venue account — reused from `test_sources_instagram_live.py`
(same live-test discipline as ADR 0006's static-post test). The URL is
documented below so a future maintainer knows why it was chosen and what to
refresh when Meta removes the post.

Assertions are deliberately loose (`status in {"ok", "needs_clarification",
"error"}`) because the STRONG-tier LLM's exact terminal branch is not
deterministic — the test locks the *shape* of the outcome and the audit
trail, not the specific value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from planazo.agents.extractor import extract_once
from planazo.extraction.audit import default_extraction_log_path
from planazo.identity import get_or_create_user
from planazo.memory import facts, rules
from planazo.storage import db

# Sala Apolo — one of Barcelona's oldest and best-known music venues.
# Same target as `test_sources_instagram_live.py`. If this URL 404s, replace
# with another public static post from @sala_apolo, @razzmatazz, or
# @boyardobcn.
_LIVE_STATIC_POST_URL = "https://www.instagram.com/p/DKQ1UO0oS-b/"


@pytest.mark.live
def test_extract_once_hits_real_llm_against_public_barcelona_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One real extraction against a public Barcelona-venue static post."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    log_path = tmp_path / "extraction_runs.jsonl"
    monkeypatch.setattr("planazo.extraction.audit.default_extraction_log_path", lambda: log_path)

    conn = db.connect()
    try:
        user = get_or_create_user(conn, "tg-live-1", "Live Test User")
        assert user.id is not None
    finally:
        conn.close()

    result = extract_once(_LIVE_STATIC_POST_URL, delegator_user_id=user.id)

    assert result.status in {"ok", "needs_clarification", "error"}
    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) >= 1
    assert any('"agent":"extractor"' in line for line in lines)

    # `default_extraction_log_path` is patched but reachable — sanity check.
    assert default_extraction_log_path() == log_path
