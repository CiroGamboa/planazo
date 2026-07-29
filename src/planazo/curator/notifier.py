"""Operator daily-summary Telegram DM after each curator tick (FU3, ADR 0020).

Every curator tick (LLM `--tick` or retention `--rotate-archived`) writes
one JSONL audit line and, if the operator has configured admin recipients,
sends one Telegram DM to each admin summarizing the outcome.

Env-var contract (matches the pattern the bot already uses for its token):
- `TELEGRAM_BOT_TOKEN` — the same token the bot polls with. Reused for
  the DM send. Missing token = no DM (best-effort no-op).
- `TELEGRAM_ADMIN_USER_IDS` — comma-separated list of integer Telegram
  user ids that receive the summary. Missing or empty = no DM sent
  (no-op — the daily cron is safe to run with no operator setup).

Rule 2 discipline: message text carries only counters, ids, run_id,
and Literal-valued fields. It NEVER interpolates `Event.title`,
`description`, `venue_name`, or the LLM's `reason` argument. Full LLM
rationale stays DB-inside in `llm_decisions.rationale`.

Rule 4 discipline: every failure surface is caught + logged at WARNING.
A missing token, unreachable Telegram API, HTTP 4xx/5xx, or a malformed
env var never propagates. The tick's DB decisions and its `agent_runs` /
`llm_decisions` / `curator_runs.jsonl` writes are already committed by
the time this fires.

Uses stdlib `urllib.request` to POST to the Telegram Bot API — no async,
no new dependency. Timeout capped at `_TELEGRAM_SEND_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from planazo.curator.agent import CuratorRunResult

logger = logging.getLogger(__name__)

_TELEGRAM_BOT_TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"
_TELEGRAM_ADMIN_USER_IDS_ENV: Final[str] = "TELEGRAM_ADMIN_USER_IDS"
_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_TELEGRAM_SEND_TIMEOUT_SECONDS: Final[float] = 10.0


def notify_admins_of_tick(result: CuratorRunResult) -> None:
    """Send one summary DM per admin describing the LLM curator tick.

    Reads `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ADMIN_USER_IDS` from env.
    Missing token or empty admin list is a silent no-op — the operator
    simply hasn't set up notifications yet.

    Every send is wrapped in try/except; a failure logs a WARNING and
    returns. The caller (`curator.service.run_curator`) invokes this
    AFTER the tick's DB writes have committed.
    """
    _notify_admins(_render_tick_message(result))


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
            "curator.notifier: %s parse failed: %s",
            _TELEGRAM_ADMIN_USER_IDS_ENV,
            exc,
        )
        return
    for chat_id in admin_ids:
        try:
            _send_telegram_message(token=token, chat_id=chat_id, text=message)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.warning(
                "curator.notifier: send to chat_id=%s failed: %s",
                chat_id,
                type(exc).__name__,
            )


def _parse_admin_user_ids(raw: str) -> list[int]:
    """Parse the comma-separated `TELEGRAM_ADMIN_USER_IDS` env var.

    Each entry must be a positive integer. Whitespace around commas is
    tolerated. Empty entries (e.g. trailing comma) are skipped. A
    non-integer entry raises `ValueError` and the whole DM fan-out is
    skipped for this tick (defense against a typo silently muting the
    operator's alerts).
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
    """POST one `sendMessage` call to the Telegram Bot API.

    Uses stdlib `urllib.request`. Raises on any HTTP or network failure —
    the caller catches and swallows. `disable_web_page_preview` matters
    for the retention summary which might include a shortened repo URL
    in a future revision; keep it on now.
    """
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


def _render_tick_message(result: CuratorRunResult) -> str:
    """Build the Rule-2-safe summary text for an LLM curator tick.

    Fields interpolated: `run_id` (uuid hex, no LLM content), `stopped`
    (Literal), `steps` (int), archived/merged/updated counts (ints),
    error count (int), dry_run (bool). Nothing else.
    """
    lines = [
        "🧹 Planazo curator tick",
        f"run_id: {result.run_id[:8]}",
        f"stopped: {result.stopped}",
        f"steps: {result.steps}",
        f"archived: {result.events_archived}",
        f"merged: {result.events_merged}",
        f"categories updated: {result.categories_updated}",
        f"errors: {len(result.errors)}",
        f"dry_run: {result.dry_run}",
    ]
    return "\n".join(lines)
