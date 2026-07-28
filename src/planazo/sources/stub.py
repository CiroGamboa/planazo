"""Test-only `EventSource` stub — a canned URL → payload lookup.

`StubEventSource` conforms structurally to `interfaces.sources.EventSource`;
tests wire it into `SOURCES` in place of a live adapter so nothing in the loop
knows the difference. `fetch_post(url)` looks the URL up in the caller-supplied
`payloads` dict — a match returns the canned `RawPost` or typed error dict, a
miss returns `error_state("not_found", ...)` so the caller still exercises the
typed-branch surface.

The stub carries no network, no cookies, no adapter-specific quirks — it is
the smallest thing that satisfies the Protocol.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from planazo.sources.base import error_state
from planazo.sources.models import RawPost


@dataclass
class StubEventSource:
    """Canned `EventSource` for tests — no network, no state, no side effects."""

    name: str = "stub"
    cadence: timedelta = timedelta(hours=6)
    payloads: dict[str, RawPost | dict[str, Any]] = field(default_factory=dict)

    def fetch_post(self, url: str) -> RawPost | dict[str, Any]:
        """Return the canned payload for `url`, or a `not_found` typed error."""
        if url in self.payloads:
            return self.payloads[url]
        return error_state("not_found", f"no canned payload for {url}", url)

    def targets(self) -> Iterator[str]:
        """Iterate the URLs the stub has canned payloads for."""
        return iter(self.payloads)
