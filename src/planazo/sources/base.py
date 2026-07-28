"""Shared registry, error taxonomy, and scheduling helper for source adapters.

`SOURCES` is the module-level registry the composition root consults to look
up an `EventSource` by name — a name-keyed `dict[str, EventSource]` that
tests monkeypatch to inject fakes. Whichever caller wires an adapter (today
the container entrypoint under `planazo.sources.instagram.cli`; later the
Extraction Agent composition root) is the one that populates the dict.

`ErrorType` names the five typed error branches every `EventSource.fetch_post`
call may return (AGENTS.md rule 4: errors are typed branches, not silent
successes). `error_state(...)` is the factory adapters use so every failure
carries the same three keys — `error_type`, `message`, `url`.

`next_run_after(cadence, last_run, *, now)` is the deterministic scheduling
helper the future source-scheduler ticket will consume. `now` is a required
keyword-only callable so tests inject a fixed clock; production callers pass
`lambda: datetime.now(timezone.utc)`. When `last_run` is `None` (adapter has
never run for this target), the helper returns `now()` — run immediately —
rather than a sentinel; the caller does not have to branch on `None`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from planazo.interfaces.sources import EventSource

ErrorType = Literal[
    "unsupported_source",
    "rate_limited",
    "auth_failed",
    "not_found",
    "unsupported_media",
]

SOURCES: dict[str, EventSource] = {}


def error_state(error_type: ErrorType, message: str, url: str) -> dict[str, Any]:
    """Build the typed error dict every `EventSource.fetch_post` returns on failure.

    Every failure carries the same three keys so callers branch on
    `error_type` without knowing which adapter produced the error.
    """
    return {"error_type": error_type, "message": message, "url": url}


def next_run_after(
    cadence: timedelta,
    last_run: datetime | None,
    *,
    now: Callable[[], datetime],
) -> datetime:
    """Return the datetime at which an adapter should next run.

    `last_run=None` means the adapter has never run for this target; the
    helper returns `now()` so the scheduler runs it immediately. Otherwise
    the next run is exactly `last_run + cadence`. `now` is injected so
    tests are deterministic — there is no `datetime.now()` inside.
    """
    if last_run is None:
        return now()
    return last_run + cadence
