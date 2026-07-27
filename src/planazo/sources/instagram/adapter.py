"""`InstagramSource` — the concrete `EventSource` for Instagram.

`fetch_post` routes on `InstaloaderPostView.typename` and returns a media-
type-agnostic `RawPost`:

- `"GraphImage"` (static post) — one `MediaAsset(kind="image")`.
- `"GraphSidecar"` (carousel) — one `MediaAsset(kind="image")` per image
  node; each video node contributes a `MediaAsset(kind="video")` followed
  by a sibling `MediaAsset(kind="thumbnail")`. Media order follows
  sidecar-node order.
- `"GraphVideo"` (reel or regular video post — instaloader does not
  distinguish `/reel/` from `/tv/`) — `MediaAsset(kind="video")` first,
  `MediaAsset(kind="thumbnail")` second.

Failure modes map to the five typed error branches (AGENTS.md rule 4):

- URL not on `instagram.com/(p|reel|tv)/…` — `unsupported_source`
- `InstagramClientError(error_type=...)` from the client — passed through
- `GraphVideo` with `video_url=None` (login-walled) — `unsupported_media`
  naming "video url not resolvable"
- A `GraphSidecar` node whose `is_video` is `True` but whose `video_url`
  is missing — `unsupported_media` naming the sidecar node

The adapter never raises on the happy path; every failure is a typed dict.
`InstagramClient` is dependency-injected so tests wire a fake with the same
`fetch_metadata` surface and the adapter is exercised without the network.

`plan_for(account)` is the config-driven strategy hook the scheduler will
consume: it expands one `AccountConfig` into `(url, media_kind)` pairs
honouring the account's resolved `MediaTypeFlags`, so an account with
`reels: false` never contributes a `("<url>", "reels")` entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from planazo.sources.base import error_state
from planazo.sources.config import AccountConfig, MediaTypeFlags, SourceConfig
from planazo.sources.instagram.client import InstagramClient, InstagramClientError
from planazo.sources.instagram.model_view import (
    InstaloaderPostView,
    InstaloaderSidecarNodeView,
)
from planazo.sources.models import MediaAsset, RawPost

_INSTAGRAM_URL = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/(?P<shortcode>[^/?#]+)/?"
)

_MEDIA_TYPE_FIELDS: tuple[str, ...] = ("static_posts", "reels", "carousels", "video_posts")


class InstagramSource:
    """Concrete `EventSource` for Instagram posts."""

    name: str = "instagram"

    def __init__(self, config: SourceConfig, client: InstagramClient) -> None:
        self._config = config
        self._client = client
        self.cadence: timedelta = config.default_cadence

    def fetch_post(self, url: str) -> RawPost | dict[str, Any]:
        """Fetch one post by URL — returns `RawPost` or a typed error dict."""
        shortcode = _extract_shortcode(url)
        if shortcode is None:
            return error_state(
                "unsupported_source",
                f"not an instagram post URL: {url}",
                url,
            )
        try:
            view = self._client.fetch_metadata(shortcode)
        except InstagramClientError as exc:
            return error_state(exc.error_type, str(exc), url)
        return self._route(view, url)

    def targets(self) -> Iterator[str]:
        """Iterate the account URLs this source is configured to monitor."""
        return (account.url for account in self._config.accounts)

    def plan_for(self, account: AccountConfig) -> list[tuple[str, str]]:
        """Expand one account into `(url, media_kind)` pairs.

        Honours the account's resolved `MediaTypeFlags` — an account with
        `reels: false` in its resolved flags never contributes a `"reels"`
        entry. The scheduler consumes this to decide which fetches to run
        for the account on each cadence tick.
        """
        flags = account.resolved_media_types(self._config)
        return [(account.url, kind) for kind in _enabled_media_types(flags)]

    def _route(self, view: InstaloaderPostView, url: str) -> RawPost | dict[str, Any]:
        """Branch on `typename` — one arm per supported post shape."""
        if view.typename == "GraphImage":
            return self._as_static_post(view, url)
        if view.typename == "GraphSidecar":
            return self._as_carousel(view, url)
        if view.typename == "GraphVideo":
            return self._as_video(view, url)
        return error_state(
            "unsupported_media",
            f"post typename {view.typename!r} not handled",
            url,
        )

    def _as_static_post(self, view: InstaloaderPostView, url: str) -> RawPost:
        return RawPost(
            source=self.name,
            permalink=url,
            title=None,
            caption=view.caption,
            posted_at=view.date_utc,
            author_handle=view.owner_username,
            media=[MediaAsset(kind="image", url=view.url)],
        )

    def _as_carousel(self, view: InstaloaderPostView, url: str) -> RawPost | dict[str, Any]:
        media: list[MediaAsset] = []
        for node in view.sidecar_nodes:
            assets = _sidecar_node_assets(node)
            if assets is None:
                return error_state(
                    "unsupported_media",
                    "sidecar video node has no resolvable URL",
                    url,
                )
            media.extend(assets)
        return RawPost(
            source=self.name,
            permalink=url,
            title=None,
            caption=view.caption,
            posted_at=view.date_utc,
            author_handle=view.owner_username,
            media=media,
        )

    def _as_video(self, view: InstaloaderPostView, url: str) -> RawPost | dict[str, Any]:
        if view.video_url is None:
            return error_state(
                "unsupported_media",
                "video url not resolvable — likely login-walled",
                url,
            )
        return RawPost(
            source=self.name,
            permalink=url,
            title=None,
            caption=view.caption,
            posted_at=view.date_utc,
            author_handle=view.owner_username,
            media=[
                MediaAsset(
                    kind="video",
                    url=view.video_url,
                    duration_seconds=view.video_duration,
                ),
                MediaAsset(kind="thumbnail", url=view.url),
            ],
        )


def _extract_shortcode(url: str) -> str | None:
    """Return the shortcode from an Instagram post URL, or `None` on mismatch."""
    match = _INSTAGRAM_URL.match(url)
    if match is None:
        return None
    return match.group("shortcode")


def _sidecar_node_assets(node: InstaloaderSidecarNodeView) -> list[MediaAsset] | None:
    """Build the `MediaAsset` list for one sidecar node.

    Image node → `[MediaAsset(kind="image")]`. Video node with a resolvable
    `video_url` → `[MediaAsset(kind="video"), MediaAsset(kind="thumbnail")]`
    in that order. Video node with `video_url=None` → `None` so the caller
    can escalate to an `unsupported_media` typed error naming the sidecar.
    """
    if not node.is_video:
        return [MediaAsset(kind="image", url=node.display_url)]
    if node.video_url is None:
        return None
    return [
        MediaAsset(
            kind="video",
            url=node.video_url,
            duration_seconds=node.video_duration,
        ),
        MediaAsset(kind="thumbnail", url=node.display_url),
    ]


def _enabled_media_types(flags: MediaTypeFlags) -> list[str]:
    """Return the enabled media-type field names in declaration order."""
    return [name for name in _MEDIA_TYPE_FIELDS if getattr(flags, name)]
