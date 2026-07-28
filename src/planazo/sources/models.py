"""Pydantic v2 payload models for the source-adapter context.

`RawPost` and `MediaAsset` are the media-type-agnostic shape every
`EventSource.fetch_post` returns on the happy path (typed error dicts cover the
failure branches — see `planazo.sources.base.error_state`). Static Instagram
posts, reels, carousels, and video posts all fit these models unchanged; a
future TikTok / YouTube / news adapter drops into the same shape.

The models conform structurally to `planazo.interfaces.sources.RawPost` and
`planazo.interfaces.sources.MediaAsset` — the Protocols name the shape,
`models.py` names the concrete Pydantic that satisfies it (AGENTS.md rule 1:
every payload from a scraped page or third-party API passes through a
Pydantic v2 schema at the boundary).

Pydantic v2 accepts an ISO-8601 string at `model_validate` time and stores a
`datetime` on `RawPost.posted_at`; no explicit field validator is needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MediaAsset(BaseModel):
    """One image / video / thumbnail attached to a `RawPost`.

    `kind` is the discriminator the Extractor (M3) branches on when it decides
    how to probe the multimodal LLM. `url` is the last-known-good source URL —
    the adapter never downloads binary content; the Extractor pulls the media
    on demand. `duration_seconds` is populated for videos only; `width` and
    `height` are populated when the source exposes them.
    """

    kind: Literal["image", "video", "thumbnail"]
    url: str = Field(min_length=1)
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


class RawPost(BaseModel):
    """One post fetched from a source adapter — the shape the Extractor consumes.

    Structurally identical for every source: `source` names the adapter
    (`"instagram"`, `"tiktok"`, `"youtube"`, ...), `permalink` is the canonical
    URL of the post, `media` is the ordered list of assets attached to it.
    `title` and `author_handle` are optional because not every source exposes
    them (Instagram has no title; some news pages have no author handle).

    `posted_at` accepts an ISO-8601 string at `model_validate` time and stores
    a `datetime` — Pydantic v2 handles the parse without a custom validator.
    """

    source: str = Field(min_length=1)
    permalink: str = Field(min_length=1)
    title: str | None = None
    caption: str | None = None
    posted_at: datetime
    author_handle: str | None = None
    media: list[MediaAsset] = Field(default_factory=list)
