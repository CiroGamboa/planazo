"""Pydantic-validated projection of the instaloader `Post` fields we consume.

`InstaloaderPostView` is the third-party boundary layer between
`instaloader.Post` (a Python object with loose attribute typing on top of
Instagram's GraphQL response) and the adapter's domain-shaped `RawPost`.
Every field the adapter reads goes through this view so a schema drift on
Meta's side surfaces as a `ValidationError` at fetch time rather than as a
missing-attribute error deep inside the adapter (AGENTS.md rule 1).

Field selection tracks what Stage 2 + Stage 3 consume:

- `shortcode`, `typename`, `owner_username`, `caption`, `date_utc` — the
  post-header fields shared by every post type.
- `url` — the display image URL used for static posts (`GraphImage`) and
  as the thumbnail for videos and video-sidecar nodes.
- `video_url`, `video_duration` — populated for `GraphVideo`; the adapter
  branches on their presence.
- `mediacount`, `sidecar_nodes` — populated for `GraphSidecar` carousels;
  Stage 3 iterates the nodes.

`typename` is a `Literal` of the three post-shape names instaloader emits so
an unrecognized shape is rejected at validate time instead of routed to
`unsupported_media` inside the adapter — the two failure modes are
distinct: a schema drift (Meta added a new post kind) versus an existing
kind we do not yet handle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InstaloaderSidecarNodeView(BaseModel):
    """One node of a `GraphSidecar` carousel.

    Stage 3 iterates these to build one `MediaAsset` per node. Stage 2 does
    not read them but the field still validates when populated so the shape
    is correct end-to-end from the day the wrapper lands.
    """

    model_config = ConfigDict(extra="ignore")

    is_video: bool = False
    display_url: str = Field(min_length=1)
    video_url: str | None = None
    video_duration: float | None = None


class InstaloaderPostView(BaseModel):
    """Validated subset of `instaloader.Post` — the seam between scraper and adapter."""

    model_config = ConfigDict(extra="ignore")

    shortcode: str = Field(min_length=1)
    typename: Literal["GraphImage", "GraphVideo", "GraphSidecar"]
    caption: str | None = None
    date_utc: datetime
    owner_username: str = Field(min_length=1)
    url: str = Field(min_length=1)
    video_url: str | None = None
    video_duration: float | None = None
    mediacount: int = 1
    sidecar_nodes: list[InstaloaderSidecarNodeView] = Field(default_factory=list)
