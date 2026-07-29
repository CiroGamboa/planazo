"""Stdout narrative logger for the demo command `planazo-scheduler --once`.

`NarrativeLogger` prints one human-readable line per extraction phase to
stdout — a decorative signal for a live demo audience, layered on top of
the JSONL sidecar (`var/extraction_runs.jsonl`) that remains the source of
truth. The logger is opt-in via `--verbose`; cron ticks do not construct
one, keeping the historical one-line-per-URL output shape unchanged.

**Rule 2 discipline.** The narrative stream is stdout-outside: log lines
carry only URLs, shortcodes, and structural signals (media count, event
index, category, confidence, status literals, error-type literals). It
never interpolates `LLMDecision.rationale`, `Event.title`,
`Event.description`, `RawPost.caption`, or any other LLM-derived or
user-generated string. The extractor's DB-inside audit surface
(`agent_runs` + `llm_decisions`) still carries the full sanitized text
per [ADR 0015](../../docs/adr/0015-storage-migrations-and-observability.md).

The observer is a plain `Callable[[StepRecord], None]` compatible with
`agents/loop.py`'s `on_step` seam — `extract_once` chains it alongside
the existing `ExtractionRunLogger`. See
[ADR 0017](../../docs/adr/0017-instagram-demo-narrative-logs.md) for the
opt-in / stdout-only / structural-signals decisions.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final, TextIO

from planazo.agents.loop import LoopResult, StepRecord

logger = logging.getLogger(__name__)

_SHORTCODE_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/(?P<shortcode>[A-Za-z0-9_-]+)"
)


def _shortcode_from_url(url: str) -> str:
    """Extract the shortcode from an Instagram post URL, or return `"(unknown)"`.

    The narrative log's setup line names the shortcode so the operator can
    correlate stdout with the JSONL trace. A non-Instagram URL or a
    malformed one falls back to a stable placeholder rather than raising —
    Rule 4 discipline extends to the display layer (a narrative print
    must never take down a live demo).
    """
    match = _SHORTCODE_RE.match(url)
    if match is None:
        return "(unknown)"
    return match.group("shortcode")


def _hhmmss(now: datetime) -> str:
    """Format `datetime` as `HH:MM:SS` with leading zeros — always UTC-aware.

    Kept module-private so tests can rebuild the exact format when
    asserting line shape without importing `datetime` transitively.
    """
    return now.strftime("%H:%M:%S")


class NarrativeLogger:
    """Prints one human-readable line per extraction phase to stdout.

    Constructed once per `--verbose` `planazo-scheduler --once` invocation.
    `start()` prints the setup line; `__call__(record)` dispatches on tool
    name to print per-step lines; `complete(loop_result)` prints the
    terminal line. Every method is best-effort — any exception during
    formatting is logged at WARNING and swallowed, matching the discipline
    of `AgentRunLogger` and the module-level `logger.warning` surface.

    Rule 2 discipline: log lines interpolate only URLs, shortcodes,
    integers, floats, and Literal-valued fields (`status`, `error_type`,
    `category`, `stopped`). No `Event.title`, no `event.description`, no
    `RawPost.caption`, no `LLMDecision.rationale`, no `notes` string ever
    reaches this stream.
    """

    def __init__(self, *, url: str, stream: TextIO | None = None) -> None:
        self._url = url
        self._stream = stream if stream is not None else sys.stdout

    # ------------------------------------------------------------------
    # Public entry points — start / __call__ / complete
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Print the setup line: `[HH:MM:SS] Fetching post <shortcode> from Instagram...`.

        Called by the composition root (the scheduler CLI) before
        `extract_once` runs. Best-effort — a stdout failure or a URL that
        fails to parse must not break the extraction.
        """
        try:
            shortcode = _shortcode_from_url(self._url)
            self._emit(f"Fetching post {shortcode} from Instagram...")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("narrative_logger start failed: %s", exc)

    def __call__(self, record: StepRecord) -> None:
        """Format one step and print it. Swallows any exception.

        Dispatches on `record.tool`:
        - `fetch_instagram_post` → `[HH:MM:SS] Fetched post — <n> media asset(s)`
          derived from the count of `media` in the tool result.
        - `save_event` → `[HH:MM:SS] Saved event at index <i> - category=<c>, confidence=<s>`
          derived from `record.arguments` (LLM-supplied structural fields
          only — no `title`, no `description`).
        - `report_extraction_status` → `[HH:MM:SS] Reported <status>: <error_type>`
          derived from Literal-typed argument fields.
        - Anything else (including `fetch_reel_frames` if a future
          refactor lifts frame extraction into a tool) is silently
          skipped — the narrative stream does not need to enumerate every
          possible future tool.

        `record.result` on the fetch branch may be a typed error dict
        (`{"error_type": ..., "message": ..., "url": ...}`); we branch on
        the `error_type` key and print a structural error line rather
        than reaching into `message` (which could echo an upstream API
        payload).
        """
        try:
            if record.tool == "fetch_instagram_post":
                self._emit_fetch(record)
            elif record.tool == "save_event":
                self._emit_save(record)
            elif record.tool == "report_extraction_status":
                self._emit_report(record)
        except Exception as exc:
            logger.warning("narrative_logger step failed: %s", exc)

    def complete(self, loop_result: LoopResult) -> None:
        """Print the terminal line: `[HH:MM:SS] Loop terminated: stopped=<s>, steps=<n>`.

        Called by the composition root at the end of `extract_once` with
        the final `LoopResult`. Only the Literal-valued `stopped` and the
        integer `steps` are interpolated; `loop_result.answer` (a free-
        form LLM string) is never printed.
        """
        try:
            self._emit(f"Loop terminated: stopped={loop_result.stopped}, steps={loop_result.steps}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("narrative_logger complete failed: %s", exc)

    # ------------------------------------------------------------------
    # Per-tool formatters — Rule 2 audit surface lives here.
    # ------------------------------------------------------------------

    def _emit_fetch(self, record: StepRecord) -> None:
        result = record.result
        if isinstance(result, dict) and "error_type" in result:
            # Typed adapter error — Literal-valued `error_type`, safe to print.
            # `message` is NOT printed — it could carry an upstream body.
            error_type = result.get("error_type", "unknown")
            self._emit(f"Fetch failed: error_type={error_type}")
            return
        media_count = 0
        if isinstance(result, dict):
            media = result.get("media")
            if isinstance(media, list):
                media_count = len(media)
        self._emit(f"Fetched post - {media_count} media asset(s)")

    def _emit_save(self, record: StepRecord) -> None:
        args: dict[str, Any] = record.arguments or {}
        # These three arguments are structural signals only.
        # `title` / `description` / `notes` / `venue_*` are NOT read here.
        event_index = args.get("event_index_in_post", 0)
        category = args.get("category", "(none)")
        confidence = args.get("confidence", 0.0)
        # Rule 2: mypy narrows here but we defensively coerce to safe primitives
        # rather than trust the arg dict — an odd float returns as-is via !r.
        try:
            confidence_float = float(confidence)
            confidence_repr = f"{confidence_float:.2f}"
        except (TypeError, ValueError):
            confidence_repr = "(invalid)"
        # If save_event failed, print the failure branch (structural only).
        result = record.result
        if isinstance(result, dict) and "error_type" in result:
            error_type = result.get("error_type", "unknown")
            self._emit(f"Save failed at index {event_index}: error_type={error_type}")
            return
        self._emit(
            f"Saved event at index {event_index} - "
            f"category={category}, confidence={confidence_repr}"
        )

    def _emit_report(self, record: StepRecord) -> None:
        args: dict[str, Any] = record.arguments or {}
        # `status` and `error_type` are Literal-valued; `notes` is NOT printed.
        status = args.get("status", "(unknown)")
        error_type = args.get("error_type", "(unknown)")
        self._emit(f"Reported {status}: {error_type}")

    # ------------------------------------------------------------------
    # Low-level emit + timestamp — one seam so tests can pin a clock.
    # ------------------------------------------------------------------

    def _emit(self, message: str) -> None:
        """Format one line with the current wall-clock timestamp and print it.

        `datetime.now(UTC)` is called per-line so each timestamp reflects
        the moment of emission, not the logger's construction time. UTC
        is used to match the JSONL sidecar's timestamp discipline; a
        future TZ-aware line-shaping decision would land in a follow-up.
        """
        line = f"[{_hhmmss(datetime.now(UTC))}] {message}"
        print(line, file=self._stream, flush=True)
