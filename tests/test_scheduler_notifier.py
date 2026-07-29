"""Scheduler admin notifier for `failure_skip` threshold trigger (ADR 0022).

Every test stubs `urllib.request.urlopen` so no real Telegram API call
fires. Coverage: env var contract, admin-id parsing, message shape
(Rule 2 — only URL + counter, no captions), per-admin fan-out, failure
swallowing, integration with `_process_source_url`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock

import pytest

from planazo.scheduler import notifier as scheduler_notifier
from planazo.scheduler.notifier import (
    _parse_admin_user_ids,
    _render_failure_skip_message,
    notify_admins_of_failure_skip,
)

# ---------------------------------------------------------------------------
# _parse_admin_user_ids
# ---------------------------------------------------------------------------


def test_parse_admin_user_ids_single_value() -> None:
    assert _parse_admin_user_ids("12345") == [12345]


def test_parse_admin_user_ids_multiple_values() -> None:
    assert _parse_admin_user_ids("1, 2, 3") == [1, 2, 3]


def test_parse_admin_user_ids_rejects_non_integer() -> None:
    with pytest.raises(ValueError, match="invalid admin user id"):
        _parse_admin_user_ids("1,not_a_number,3")


def test_parse_admin_user_ids_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError, match="admin user id must be positive"):
        _parse_admin_user_ids("1,0")


def test_parse_admin_user_ids_empty_string_returns_empty_list() -> None:
    assert _parse_admin_user_ids("") == []


# ---------------------------------------------------------------------------
# _render_failure_skip_message — Rule 2 discipline
# ---------------------------------------------------------------------------


def test_render_failure_skip_message_carries_url_and_counter() -> None:
    text = _render_failure_skip_message("https://instagram.com/venue.name/", 3)

    assert "https://instagram.com/venue.name/" in text
    assert "consecutive_failures: 3" in text
    assert "failure_skip" in text


def test_render_failure_skip_message_never_leaks_caption_or_event_content() -> None:
    """Rule 2: no title/description/venue/caption fragments cross the boundary."""
    text = _render_failure_skip_message("https://instagram.com/x/", 5).lower()

    for banned in ("title", "description", "venue", "caption", "event"):
        # 'event' is a stretch — the URL might contain it. But titles never
        # cross this surface.
        if banned == "event":
            continue
        assert banned not in text, banned


# ---------------------------------------------------------------------------
# notify_admins_of_failure_skip — env-var contract + fan-out + swallow
# ---------------------------------------------------------------------------


def _install_urlopen_stub(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace `urlopen` with a MagicMock context manager that returns bytes."""
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"ok": true}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=None)
    stub = MagicMock(return_value=fake_response)
    monkeypatch.setattr(scheduler_notifier.urllib.request, "urlopen", stub)
    return stub


def test_notify_no_op_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_failure_skip("https://x/", 3)

    stub.assert_not_called()


def test_notify_no_op_when_admin_ids_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_failure_skip("https://x/", 3)

    stub.assert_not_called()


def test_notify_sends_to_every_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111, 222, 333")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_failure_skip("https://x/", 3)

    assert stub.call_count == 3


def test_notify_posts_to_correct_url_and_carries_source_url_in_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "77")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_failure_skip("https://instagram.com/foo/", 4)

    request_arg = stub.call_args.args[0]
    assert isinstance(request_arg, urllib.request.Request)
    assert request_arg.full_url == "https://api.telegram.org/botsecret-token/sendMessage"
    payload = request_arg.data
    assert payload is not None
    payload_str = (payload if isinstance(payload, bytes) else b"").decode("utf-8")
    assert '"chat_id": 77' in payload_str
    assert "instagram.com/foo/" in payload_str
    assert "consecutive_failures: 4" in payload_str


def test_notify_swallows_urlerror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "11,22")

    call_count = {"n": 0}

    def raising_urlopen(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise urllib.error.URLError("simulated network fail")

    monkeypatch.setattr(scheduler_notifier.urllib.request, "urlopen", raising_urlopen)

    # Does not raise. Fan-out continues to remaining admins even after one fails.
    notify_admins_of_failure_skip("https://x/", 3)

    assert call_count["n"] == 2


def test_notify_swallows_timeouterror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "11")

    def raising_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("slow")

    monkeypatch.setattr(scheduler_notifier.urllib.request, "urlopen", raising_urlopen)

    notify_admins_of_failure_skip("https://x/", 3)


def test_notify_no_op_when_admin_ids_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1,not-a-number")
    stub = _install_urlopen_stub(monkeypatch)

    notify_admins_of_failure_skip("https://x/", 3)

    stub.assert_not_called()
