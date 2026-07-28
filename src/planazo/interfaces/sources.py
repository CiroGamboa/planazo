"""The event-source adapter contract.

Swap axis: Instagram (first concrete), TikTok, YouTube, news pages,
Meetup, Eventbrite — every source drops into this slot without changing
the interface. The concrete Instagram implementation lives in
`planazo.sources.instagram`; the shape here is what every `EventSource`
adapter satisfies. See
[ADR 0006 — Instagram extraction approach](../../../docs/adr/0006-instagram-extraction-approach.md)
for the scraper choice + container + rate-limit envelope + session policy.

`RawPost` + `MediaAsset` are the media-type-agnostic payload every adapter
returns: static posts, reels, carousels, and video posts all fit the same
Pydantic v2 model. The Extractor branches on `media[*].kind` when it
probes the multimodal LLM.

This module forward-declares the shapes without importing concrete Pydantic
models — those live in `planazo.sources.models`. Downstream milestones type
their consumers against these Protocols so the four axes swap independently.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol


class MediaAsset(Protocol):
    """One image / video / thumbnail attached to a `RawPost`.

    `kind` is the discriminator the Extractor branches on. Adapters produce
    `MediaAsset`-shaped objects; concrete Pydantic implementations live in
    `planazo.sources.models`.
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
    The error-branch taxonomy is `unsupported_source`, `not_found`,
    `rate_limited`, `auth_failed`, `unsupported_media` — see
    [ADR 0006](../../../docs/adr/0006-instagram-extraction-approach.md).
    """

    name: str  # `"instagram"`, `"tiktok"`, ...
    cadence: timedelta  # how often the scheduler should re-run this source

    def fetch_post(self, url: str) -> RawPost | dict[str, Any]:
        """Fetch one post by URL; return the `RawPost` or a typed error dict."""
        ...

    def targets(self) -> Iterator[str]:
        """Iterate the URLs this source is configured to monitor."""
        ...
