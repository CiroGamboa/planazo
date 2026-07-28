"""Instagram source adapter — the first concrete `EventSource`.

`InstagramSource` fetches one post at a time by URL through a thin
`InstagramClient` wrapper around `instaloader`. The client is the swap point
if a future ADR replaces the scraper; the adapter itself only depends on the
Pydantic `InstaloaderPostView` the client returns.

The five typed error branches every `fetch_post` may return come from
`planazo.sources.base` (AGENTS.md rule 4); the client-to-adapter reconciliation
of instaloader's pinned exception surface is in `client.py`.
"""

from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.tools import build_fetch_instagram_post

__all__ = ["InstagramSource", "build_fetch_instagram_post"]
