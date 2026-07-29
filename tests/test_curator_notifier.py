"""Operator daily-summary Telegram DM (FU3, ADR 0020 follow-up).

Every test stubs `urllib.request.urlopen` so no real Telegram API call
fires. Coverage: env var contract, admin-id parsing edge cases,
message shape (Rule 2 — only ids + counts + Literals), per-admin
fan-out, failure swallowing, wiring into `run_curator`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from planazo.curator import notifier as curator_notifier
from planazo.curator.agent import CuratorRunResult
from planazo.curator.notifier import (
    _parse_admin_user_ids,
    _render_retention_message,
    _render_tick_message,
    notify_admins_of_retention,
    notify_admins_of_tick,
)
from planazo.curator.retention import RetentionResult


def _tick_result(**overrides: Any) -> CuratorRunResult:
    defaults: dict[str, Any] = {
        "run_id": "abcdef01-run-id-1234",
        "stopped": "answered",
        "steps": 5,
        "events_examined": 12,
        "events_archived": 3,
        "events_merged": 1,
        "categories_updated": 2,
        "errors": [],
        "dry_run": False,
        "started_at": datetime(2026, 12, 1, 3, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 12, 1, 3, 0, 30, tzinfo=UTC),
    }
    defaults.update(overrides)
    return CuratorRunResult(**defaults)


# ---------------------------------------------------------------------------
# _parse_admin_user_ids
# ---------------------------------------------------------------------------


def test_parse_admin_user_ids_single_value() -> None:
    assert _parse_admin_user_ids("12345") == [12345]


def test_parse_admin_user_ids_multiple_values() -> None:
    assert _parse_admin_user_ids("1, 2, 3") == [1, 2, 3]


def test_parse_admin_user_ids_tolerates_trailing_comma() -> None:
    assert _parse_admin_user_ids("1, 2,") == [1, 2]


def test_parse_admin_user_ids_tolerates_whitespace() -> None:
    assert _parse_admin_user_ids("  1  ,  2  ") == [1, 2]


def test_parse_admin_user_ids_rejects_non_integer() -> None:
    with pytest.raises(ValueError, match="invalid admin user id"):
        _parse_admin_user_ids("1,not_a_number,3")


def test_parse_admin_user_ids_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError, match="admin user id must be positive"):
        _parse_admin_user_ids("1,0,3")
    with pytest.raises(ValueError, match="admin user id must be positive"):
        _parse_admin_user_ids("-5")


def test_parse_admin_user_ids_empty_string_returns_empty_list() -> None:
    assert _parse_admin_user_ids("") == []


# ---------------------------------------------------------------------------
# _render_tick_message — Rule 2 discipline
# ---------------------------------------------------------------------------


def test_render_tick_message_carries_all_counters() -> None:
    text = _render_tick_message(_tick_result())

    assert "run_id: abcdef01" in text
    assert "stopped: answered" in text
    assert "steps: 5" in text
    assert "archived: 3" in text
    assert "merged: 1" in text
    assert "categories updated: 2" in text
    assert "errors: 0" in text
    assert "dry_run: False" in text


def test_render_tick_message_never_leaks_llm_content() -> None:
    """Rule 2: no title, description, venue, or reason strings interpolated."""
    result = _tick_result(errors=["not_found: no event with id=999"])
    text = _render_tick_message(result)

    # `errors` in the message is only the count — the actual error
    # strings live in agent_runs / llm_decisions / var/curator_runs.jsonl.
    assert "not_found" not in text
    assert "no event with id" not in text
    # Curator run_ids are UUIDs; nothing else from Event or LLM state.
    for banned in ("title", "description", "venue", "caption"):
        assert banned not in text.lower()


# ---------------------------------------------------------------------------
# notify_admins_of_tick — env-var contract + fan-out + failure swallow
# ---------------------------------------------------------------------------


def _install_urlopen_stub(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace `urlopen` with a MagicMock context manager that returns bytes."""
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"ok": true}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=None)
    stub = MagicMock(return_value=fake_response)
    monkeypatch.setattr(curator_notifier.urllib.request, "urlopen", stub)
    return stub


def test_notify_admins_of_tick_no_op_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1,2")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    stub.assert_not_called()


def test_notify_admins_of_tick_no_op_when_admin_ids_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    stub.assert_not_called()


def test_notify_admins_of_tick_no_op_when_admin_ids_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "   ")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    stub.assert_not_called()


def test_notify_admins_of_tick_sends_to_every_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111, 222, 333")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    assert stub.call_count == 3


def test_notify_admins_of_tick_posts_to_correct_url_and_chat_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "77")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    request_arg = stub.call_args.args[0]
    assert isinstance(request_arg, urllib.request.Request)
    assert request_arg.full_url == "https://api.telegram.org/botsecret-token/sendMessage"
    # Verify the payload carries our chat_id.
    payload = request_arg.data
    assert payload is not None
    payload_str = (payload if isinstance(payload, bytes) else b"").decode("utf-8")
    assert '"chat_id": 77' in payload_str
    assert '"disable_web_page_preview": true' in payload_str


def test_notify_admins_of_tick_swallows_urlerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "11,22")

    call_count = {"n": 0}

    def raising_urlopen(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise urllib.error.URLError("simulated network fail")

    monkeypatch.setattr(curator_notifier.urllib.request, "urlopen", raising_urlopen)

    # Does not raise, and continues fanning out to remaining admins.
    notify_admins_of_tick(_tick_result())

    assert call_count["n"] == 2


def test_notify_admins_of_tick_swallows_timeouterror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "11")

    def raising_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("slow Telegram")

    monkeypatch.setattr(curator_notifier.urllib.request, "urlopen", raising_urlopen)

    # No exception propagates.
    notify_admins_of_tick(_tick_result())


def test_notify_admins_of_tick_no_op_when_admin_ids_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1,not-a-number")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_tick(_tick_result())

    # The whole fan-out is skipped when the env var can't be parsed — no
    # partial DMs to admin-before-the-typo.
    stub.assert_not_called()


# ---------------------------------------------------------------------------
# Integration with run_curator
# ---------------------------------------------------------------------------


def test_run_curator_invokes_notifier_after_upsert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The composition root fires the notifier after audit + state writes."""
    from planazo.agents.loop import LoopResult
    from planazo.curator import agent as curator_agent
    from planazo.curator.service import run_curator

    # Stub run_loop and observability so run_curator_once doesn't touch DB.
    def fake_run_loop(**kwargs: Any) -> LoopResult:
        return LoopResult(answer="done", steps=1, stopped="answered")

    monkeypatch.setattr(curator_agent, "run_loop", fake_run_loop)

    notify_calls: list[CuratorRunResult] = []

    def fake_notify(result: CuratorRunResult) -> None:
        notify_calls.append(result)

    from planazo.curator import service as curator_service

    monkeypatch.setattr(curator_service, "notify_admins_of_tick", fake_notify)

    result = run_curator(
        record_runs=False,
        audit_log_path=tmp_path / "curator_runs.jsonl",
    )

    assert len(notify_calls) == 1
    assert notify_calls[0].run_id == result.run_id


def test_run_curator_swallows_notifier_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A notifier raise never blocks the tick's return."""
    from planazo.agents.loop import LoopResult
    from planazo.curator import agent as curator_agent
    from planazo.curator.service import run_curator

    def fake_run_loop(**kwargs: Any) -> LoopResult:
        return LoopResult(answer="done", steps=1, stopped="answered")

    monkeypatch.setattr(curator_agent, "run_loop", fake_run_loop)

    def raising_notify(result: CuratorRunResult) -> None:
        raise RuntimeError("simulated notifier explosion")

    from planazo.curator import service as curator_service

    monkeypatch.setattr(curator_service, "notify_admins_of_tick", raising_notify)

    # Does not raise — the tick's DB decisions are already committed.
    result = run_curator(
        record_runs=False,
        audit_log_path=tmp_path / "curator_runs.jsonl",
    )
    assert result.stopped == "answered"


# ---------------------------------------------------------------------------
# Retention notifier
# ---------------------------------------------------------------------------


def _retention_result(**overrides: Any) -> RetentionResult:
    defaults: dict[str, Any] = {
        "run_id": "cafebabe-run-id",
        "retention_days": 30,
        "cutoff": datetime(2026, 11, 1, tzinfo=UTC),
        "deleted": 3,
        "preview": [],
        "dry_run": False,
        "started_at": datetime(2026, 12, 1, tzinfo=UTC),
        "ended_at": datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return RetentionResult(**defaults)


def test_render_retention_message_carries_all_fields() -> None:
    text = _render_retention_message(_retention_result())

    assert "run_id: cafebabe" in text
    assert "retention_days: 30" in text
    assert "deleted: 3" in text
    assert "dry_run: False" in text


def test_render_retention_message_never_leaks_event_content() -> None:
    """Rule 2: no title/description/venue crosses the boundary."""
    text = _render_retention_message(_retention_result())

    for banned in ("title", "description", "venue", "caption"):
        assert banned not in text.lower()


def test_notify_admins_of_retention_sends_to_every_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111,222")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_retention(_retention_result())

    assert stub.call_count == 2


def test_notify_admins_of_retention_no_op_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_retention(_retention_result())

    stub.assert_not_called()


def test_run_retention_invokes_notifier(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The retention composition root fires notify_admins_of_retention."""
    from planazo.curator.retention import run_retention

    notify_calls: list[RetentionResult] = []

    def fake_notify(result: RetentionResult) -> None:
        notify_calls.append(result)

    from planazo.curator import retention as curator_retention

    monkeypatch.setattr(curator_retention, "notify_admins_of_retention", fake_notify)
    # Redirect the retention DB to a fresh :memory: — we don't care about
    # actual events here, just the notifier fire.
    from planazo.storage import db

    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    result = run_retention(
        retention_days=30,
        dry_run=True,
        audit_log_path=tmp_path / "curator_runs.jsonl",
    )

    assert len(notify_calls) == 1
    assert notify_calls[0].run_id == result.run_id


def test_run_retention_swallows_notifier_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A notifier raise never blocks the retention return."""
    from planazo.curator.retention import run_retention

    def raising_notify(result: RetentionResult) -> None:
        raise RuntimeError("simulated notifier explosion")

    from planazo.curator import retention as curator_retention

    monkeypatch.setattr(curator_retention, "notify_admins_of_retention", raising_notify)
    from planazo.storage import db

    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    # Does not raise — the DELETE has already committed.
    result = run_retention(
        retention_days=30,
        dry_run=True,
        audit_log_path=tmp_path / "curator_runs.jsonl",
    )
    assert result.retention_days == 30
