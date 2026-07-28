"""Pydantic-validated projection of the instaloader `Post` fields we consume.

`InstaloaderPostView` is the third-party boundary layer between
`instaloader.Post` (a Python object with loose attribute typing on top of
Instagram's GraphQL response) and the adapter's domain-shaped `RawPost`.
Every field the adapter reads goes through this view so a schema drift on
Meta's side surfaces as a `ValidationError` at fetch time rather than as a
missing-attribute error deep inside the adapter (AGENTS.md rule 1).

Field selection covers the subset the adapter reads from instaloader Posts:

- `shortcode`, `typename`, `owner_username`, `caption`, `date_utc` — the
  post-header fields shared by every post type.
- `url` — the display image URL used for static posts (`GraphImage`) and
  as the thumbnail for videos and video-sidecar nodes.
- `video_url`, `video_duration` — populated for `GraphVideo`; the adapter
  branches on their presence.
- `mediacount`, `sidecar_nodes` — populated for `GraphSidecar` carousels;
  the adapter iterates the nodes to build one `MediaAsset` per node.

`typename` validates as an open `str`: the boundary layer enforces
*structure* (the field is present and stringly-typed), not *value-space*.
The adapter routes on the string in `_route`; unknown values (a
hypothetical `GraphAudio`, or whatever Meta ships next) are returned as
`unsupported_media` naming the value, so a schema drift surfaces as a
typed error at fetch time rather than swallowing the request as a
`ValidationError` in the client.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class InstaloaderSidecarNodeView(BaseModel):
    """One node of a `GraphSidecar` carousel.

    The adapter iterates these when the post is a `GraphSidecar` to build
    one `MediaAsset` per node; when the post is not a sidecar, the field
    validates as empty.
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
    # Open string, not a Literal: the adapter routes `GraphImage` /
    # `GraphSidecar` / `GraphVideo` and returns `unsupported_media` for
    # anything else. See the module docstring for the rationale.
    typename: str = Field(min_length=1)
    caption: str | None = None
    date_utc: datetime
    owner_username: str = Field(min_length=1)
    url: str = Field(min_length=1)
    video_url: str | None = None
    video_duration: float | None = None
    mediacount: int = 1
    sidecar_nodes: list[InstaloaderSidecarNodeView] = Field(default_factory=list)
