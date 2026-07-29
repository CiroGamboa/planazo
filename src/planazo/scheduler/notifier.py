"""Admin notifier for the scheduler's `failure_skip` threshold trigger (ADR 0022).

The scheduler's second trigger type — a threshold. `_scheduler_gate` fires
`failure_skip` when `scan_state.consecutive_failures >= 3` (ADR 0011 §D9):
the URL is silently skipped for that tick, the counter resets, and the
next tick will re-attempt. This module turns the threshold crossing into
an observable event — one Telegram DM per admin, per skip — so an
operator learns that an Instagram account has been failing repeatedly
without having to `tail -f var/scheduler_runs.jsonl`.

Env-var contract (identical to `curator.notifier`):

- `TELEGRAM_BOT_TOKEN` — the same token the bot polls with. Missing → no
  DM (best-effort no-op).
- `TELEGRAM_ADMIN_USER_IDS` — comma-separated integer Telegram user ids
  that receive the alert. Missing or empty → no DM sent (a fresh deploy
  is safe to run without operator setup).

Rule 2 discipline: the message text carries only the URL and the
integer counter — no Instagram caption, no venue text, no LLM-produced
rationale. Both come from the scheduler's own scoped state, not from
any tool result.

Rule 4 discipline: every send is wrapped in try/except. A URLError,
TimeoutError, or malformed env var never propagates. The tick's
primary flow — the empty `SchedulerRunRecord` with `gate_reason="failure_skip"`
and the `scan_state.consecutive_failures` reset — is already
committed by the time this fires.

Uses stdlib `urllib.request` to POST to the Telegram Bot API — no
async, no new dependency. Timeout capped at
`_TELEGRAM_SEND_TIMEOUT_SECONDS`.

Cadence expectations (ADR 0011 §D9 interaction): after a `failure_skip`
tick the counter resets to 0. So the operator gets one alert per
4-tick cycle for a permanently broken URL (3 fail → 1 skip+alert →
resets → 3 fail → 1 skip+alert → ...). That's ~1 alert per 24 hours
at the default 6h cadence — a reasonable alert rate, not spam.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Final

logger = logging.getLogger(__name__)

_TELEGRAM_BOT_TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"
_TELEGRAM_ADMIN_USER_IDS_ENV: Final[str] = "TELEGRAM_ADMIN_USER_IDS"
_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_TELEGRAM_SEND_TIMEOUT_SECONDS: Final[float] = 10.0


def notify_admins_of_failure_skip(source_url: str, consecutive_failures: int) -> None:
    """Send one DM per admin describing a `failure_skip` gate firing.

    Called from `scheduler.service._process_source_url` right after the
    gate returns `failure_skip` and BEFORE the counter reset (so the
    counter carried in the message is the one that triggered the skip,
    not the post-reset value).

    Missing env vars → silent no-op. Every send is wrapped so a
    Telegram-side failure never propagates back into the tick.
    """
    _notify_admins(_render_failure_skip_message(source_url, consecutive_failures))


def _notify_admins(message: str) -> None:
    """Fan out one DM to every configured admin user id, best-effort."""
    token = os.environ.get(_TELEGRAM_BOT_TOKEN_ENV, "").strip()
    admin_ids_raw = os.environ.get(_TELEGRAM_ADMIN_USER_IDS_ENV, "").strip()
    if not token or not admin_ids_raw:
        return
    try:
        admin_ids = _parse_admin_user_ids(admin_ids_raw)
    except ValueError as exc:
        logger.warning(
            "scheduler.notifier: %s parse failed: %s",
            _TELEGRAM_ADMIN_USER_IDS_ENV,
            exc,
        )
        return
    for chat_id in admin_ids:
        try:
            _send_telegram_message(token=token, chat_id=chat_id, text=message)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.warning(
                "scheduler.notifier: send to chat_id=%s failed: %s",
                chat_id,
                type(exc).__name__,
            )


def _parse_admin_user_ids(raw: str) -> list[int]:
    """Parse the comma-separated `TELEGRAM_ADMIN_USER_IDS` env var.

    Same discipline as `curator.notifier._parse_admin_user_ids` — a
    non-integer entry raises `ValueError` and the whole fan-out is
    skipped for this tick so a typo silently muting the operator's
    alerts surfaces at the WARNING log rather than as a half-sent
    fan-out.
    """
    ids: list[int] = []
    for chunk in raw.split(","):
        stripped = chunk.strip()
        if not stripped:
            continue
        try:
            value = int(stripped)
        except ValueError as exc:
            raise ValueError(f"invalid admin user id {stripped!r}") from exc
        if value < 1:
            raise ValueError(f"admin user id must be positive, got {value}")
        ids.append(value)
    return ids


def _send_telegram_message(*, token: str, chat_id: int, text: str) -> None:
    """POST one `sendMessage` call to the Telegram Bot API. Raises on failure."""
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TELEGRAM_SEND_TIMEOUT_SECONDS) as response:
        response.read()


def _render_failure_skip_message(source_url: str, consecutive_failures: int) -> str:
    """Build the Rule 2-safe alert text.

    Fields interpolated: `source_url` (from `scan_state`, the scheduler's
    own scoped state — never from Extractor tool output) and
    `consecutive_failures` (int). Nothing else. In particular, no
    Instagram caption, venue text, or LLM-produced rationale ever
    crosses this boundary.
    """
    lines = [
        "⚠️ Planazo scheduler — failure_skip",
        f"url: {source_url}",
        f"consecutive_failures: {consecutive_failures}",
        "This tick is skipped; counter resets and the next tick re-attempts.",
        "See ADR 0011 §D9 for the design.",
    ]
    return "\n".join(lines)
