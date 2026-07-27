"""The event-source adapter contract.

Swap axis: Instagram (M2, first concrete), TikTok, YouTube, news pages,
Meetup, Eventbrite — every source drops into this slot without changing
the interface. The concrete Instagram implementation is scoped by
[M2 (#16)](https://github.com/CiroGamboa/planazo/issues/16) +
[ADR 0006 — Instagram extraction approach](../../../docs/adr/0006-instagram-extraction-approach.md)
(Status: Proposed until Stage 3 flips it to Accepted); the shape here
reflects what that ticket will land.

`RawPost` + `MediaAsset` are the media-type-agnostic payload every adapter
returns: static posts, reels, carousels, and video posts all fit the same
Pydantic v2 model. The Extractor (M3) branches on `media[*].kind` when it
probes the multimodal LLM.

This module intentionally forward-declares the shapes without importing
concrete Pydantic models — those live in `planazo.sources.models` and will
be added when M2 lands. Downstream milestones (M2 for the concrete adapter,
M3 for the extractor consuming it) type against these Protocols.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol


class MediaAsset(Protocol):
    """One image / video / thumbnail attached to a `RawPost`.

    `kind` is the discriminator the Extractor branches on. Adapters produce
    `MediaAsset`-shaped objects; concrete Pydantic implementations live in
    `planazo.sources.models` (added by M2).
    """

    kind: Literal["image", "video", "thumbnail"]
    url: str
    duration_seconds: float | None  # `None` for images/thumbnails
    width: int | None
    height: int | None


class RawPost(Protocol):
    """A source-agnostic payload returned by any `EventSource.fetch_post`.

    Structurally the same shape for static Instagram posts, reels,
    carousels, TikTok videos, YouTube shorts, news-page articles. The
    Extractor consumes the Protocol without knowing which adapter produced
    the result.
    """

    source: str  # `"instagram"`, `"tiktok"`, `"youtube"`, ...
    permalink: str
    title: str | None
    caption: str | None
    # Pydantic v2 auto-parses ISO-8601 strings into datetime at model_validate time
    posted_at: datetime
    author_handle: str | None
    media: list[MediaAsset]


class EventSource(Protocol):
    """One adapter fetching content from a single external source.

    Registered via a config file (`data/sources.yaml`) at startup. The
    scheduler consults `cadence` to know when to next run the adapter;
    `fetch_post` is the LLM-reachable-only-via-Extractor entry point.

    Return values on failure are typed dicts (`{"error_type": "rate_limited",
    ...}` etc.), not exceptions — the adapter never raises on the happy path.
    M2 spells out the full error-branch taxonomy.
    """

    name: str  # `"instagram"`, `"tiktok"`, ...
    cadence: timedelta  # how often the scheduler should re-run this source

    def fetch_post(self, url: str) -> RawPost | dict[str, Any]:
        """Fetch one post by URL; return the `RawPost` or a typed error dict."""
        ...

    def targets(self) -> Iterator[str]:
        """Iterate the URLs this source is configured to monitor."""
        ...
