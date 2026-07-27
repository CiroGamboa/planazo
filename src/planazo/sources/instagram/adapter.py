"""`InstagramSource` — the concrete `EventSource` for Instagram.

Static-post happy path (`InstaloaderPostView.typename == "GraphImage"`) returns
a `RawPost` with a single `MediaAsset(kind="image")`. Every other post
`typename` returns `error_state("unsupported_media", ...)` — Stage 3 widens the
type router to handle `GraphSidecar` (carousels) and `GraphVideo` (reels +
video posts).

Failure modes map to the five typed error branches (AGENTS.md rule 4):

- URL not on `instagram.com/(p|reel|tv)/…` — `unsupported_source`
- `InstagramClientError(error_type=...)` from the client — passed through
- `typename` recognised but not GraphImage (Stage 2 scope) — `unsupported_media`

The adapter never raises on the happy path; every failure is a typed dict.
`InstagramClient` is dependency-injected so tests wire a fake with the same
`fetch_metadata` surface and the adapter is exercised without the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from planazo.sources.base import error_state
from planazo.sources.config import SourceConfig
from planazo.sources.instagram.client import InstagramClient, InstagramClientError
from planazo.sources.instagram.model_view import InstaloaderPostView
from planazo.sources.models import MediaAsset, RawPost

_INSTAGRAM_URL = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/(?P<shortcode>[^/?#]+)/?"
)


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

    def _route(self, view: InstaloaderPostView, url: str) -> RawPost | dict[str, Any]:
        """Branch on `typename` — Stage 2 handles GraphImage only."""
        if view.typename == "GraphImage":
            return RawPost(
                source=self.name,
                permalink=url,
                title=None,
                caption=view.caption,
                posted_at=view.date_utc,
                author_handle=view.owner_username,
                media=[MediaAsset(kind="image", url=view.url)],
            )
        return error_state(
            "unsupported_media",
            f"post typename {view.typename!r} not handled yet",
            url,
        )


def _extract_shortcode(url: str) -> str | None:
    """Return the shortcode from an Instagram post URL, or `None` on mismatch."""
    match = _INSTAGRAM_URL.match(url)
    if match is None:
        return None
    return match.group("shortcode")
