"""Opt-in live test — one real Instagram fetch against a public Barcelona post.

Marked `live`, deselected by default (`pyproject.toml [tool.pytest.ini_options]
addopts = "-m 'not live'"`). Run explicitly:

    uv run pytest -m live tests/test_sources_instagram_live.py -v -s

The target is one hardcoded static-post URL from a well-known public
Barcelona venue account. The URL is documented in the test docstring so a
future maintainer knows why it was chosen and what to refresh when Meta
removes the post (see ADR 0006 — Risks: anonymous fetch rate-limits fast).

If this test starts failing:

- 404 / typed `not_found` → post was removed; pick another public static
  from the same venue and update the constant.
- 429 / typed `rate_limited` → repeated anonymous fetches from the same IP;
  wait or run with `INSTAGRAM_SESSION_ID` set.
- typed `auth_failed` → Instagram tightened access on the shortcode; pick
  another public static from the same or another Barcelona venue.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from planazo.sources.config import MediaTypeFlags, SourceConfig
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClient
from planazo.sources.models import RawPost

# Sala Apolo — one of Barcelona's oldest and best-known music venues.
# Instagram: @sala_apolo — public account.
# Post picked on 2026-07-28: a static-image announcement post.
# If this URL 404s, replace with another public static post from
# @sala_apolo, @razzmatazz, @boyardobcn, or another public Barcelona venue.
_LIVE_STATIC_POST_URL = "https://www.instagram.com/p/DKQ1UO0oS-b/"


@pytest.mark.live
def test_fetch_static_post_live() -> None:
    """Fetch one real public Barcelona-venue static post over the network."""
    config = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[],
    )
    client = InstagramClient()
    client.load_session_from_env()
    adapter = InstagramSource(config, client)

    result = adapter.fetch_post(_LIVE_STATIC_POST_URL)

    assert isinstance(result, RawPost), f"expected RawPost, got: {result!r}"
    assert result.source == "instagram"
    assert (
        _LIVE_STATIC_POST_URL.startswith(result.permalink)
        or result.permalink.startswith(_LIVE_STATIC_POST_URL)
        or result.permalink == _LIVE_STATIC_POST_URL
    )
    assert len(result.media) == 1
    assert result.media[0].kind == "image"
    assert isinstance(result.posted_at, datetime)
    assert result.caption is not None
