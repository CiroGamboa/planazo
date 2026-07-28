"""Unit tests for `InstagramSource` — happy paths across the three supported
`typename` shapes, the four error branches an adapter can produce, and the
config-driven `plan_for` strategy hook.

The adapter's client is injected as a fake with the same `fetch_metadata`
shape as `InstagramClient`; no network is touched. The exception-class-to-
error-type reconciliation lives in one parametrized fixture so a future
scraper-version bump changes one place instead of five tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from instaloader.exceptions import (
    LoginRequiredException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)

from planazo.sources.base import ErrorType
from planazo.sources.config import AccountConfig, MediaTypeFlags, SourceConfig
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClientError
from planazo.sources.instagram.model_view import InstaloaderPostView
from planazo.sources.models import RawPost


class _FakeClient:
    """Fake `InstagramClient` — either yields a canned view or raises on call."""

    def __init__(
        self,
        view: InstaloaderPostView | None = None,
        raises: type[BaseException] | None = None,
    ) -> None:
        self._view = view
        self._raises = raises
        self.calls: list[str] = []

    def fetch_metadata(self, shortcode: str) -> InstaloaderPostView:
        self.calls.append(shortcode)
        if self._raises is not None:
            raise self._raises("fake instaloader failure")
        assert self._view is not None
        return self._view


def _static_post_view() -> InstaloaderPostView:
    return InstaloaderPostView.model_validate(
        {
            "shortcode": "ABC123",
            "typename": "GraphImage",
            "caption": "hello barcelona",
            "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
            "owner_username": "test_venue",
            "url": "https://scontent.cdninstagram.com/i.jpg",
            "video_url": None,
            "video_duration": None,
            "mediacount": 1,
            "sidecar_nodes": [],
        }
    )


def _source_config() -> SourceConfig:
    return SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[],
    )


def test_fetch_post_static_happy_path_returns_raw_post() -> None:
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/p/ABC123/"
    result = adapter.fetch_post(url)

    assert isinstance(result, RawPost)
    assert result.source == "instagram"
    assert result.permalink == url
    assert result.caption == "hello barcelona"
    assert result.author_handle == "test_venue"
    assert result.posted_at == datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    assert len(result.media) == 1
    assert result.media[0].kind == "image"
    assert result.media[0].url == "https://scontent.cdninstagram.com/i.jpg"
    assert client.calls == ["ABC123"]


def test_fetch_post_unsupported_source_when_url_not_instagram() -> None:
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    result = adapter.fetch_post("https://twitter.com/foo")

    assert isinstance(result, dict)
    assert result["error_type"] == "unsupported_source"
    assert result["url"] == "https://twitter.com/foo"
    assert client.calls == []


def test_fetch_post_carousel_all_image_nodes_returns_one_asset_per_node() -> None:
    payload: dict[str, Any] = {
        "shortcode": "SIDE1",
        "typename": "GraphSidecar",
        "caption": "carousel",
        "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        "owner_username": "test_venue",
        "url": "https://scontent.cdninstagram.com/thumb.jpg",
        "video_url": None,
        "video_duration": None,
        "mediacount": 3,
        "sidecar_nodes": [
            {
                "is_video": False,
                "display_url": "https://scontent.cdninstagram.com/1.jpg",
                "video_url": None,
                "video_duration": None,
            },
            {
                "is_video": False,
                "display_url": "https://scontent.cdninstagram.com/2.jpg",
                "video_url": None,
                "video_duration": None,
            },
            {
                "is_video": False,
                "display_url": "https://scontent.cdninstagram.com/3.jpg",
                "video_url": None,
                "video_duration": None,
            },
        ],
    }
    view = InstaloaderPostView.model_validate(payload)
    client = _FakeClient(view=view)
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/p/SIDE1/"
    result = adapter.fetch_post(url)

    assert isinstance(result, RawPost)
    assert len(result.media) == 3
    assert [asset.kind for asset in result.media] == ["image", "image", "image"]
    assert [asset.url for asset in result.media] == [
        "https://scontent.cdninstagram.com/1.jpg",
        "https://scontent.cdninstagram.com/2.jpg",
        "https://scontent.cdninstagram.com/3.jpg",
    ]


def test_fetch_post_carousel_mixed_image_and_video_nodes_expands_video_to_pair() -> None:
    payload: dict[str, Any] = {
        "shortcode": "SIDE2",
        "typename": "GraphSidecar",
        "caption": "mixed carousel",
        "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        "owner_username": "test_venue",
        "url": "https://scontent.cdninstagram.com/thumb.jpg",
        "video_url": None,
        "video_duration": None,
        "mediacount": 3,
        "sidecar_nodes": [
            {
                "is_video": False,
                "display_url": "https://scontent.cdninstagram.com/1.jpg",
                "video_url": None,
                "video_duration": None,
            },
            {
                "is_video": True,
                "display_url": "https://scontent.cdninstagram.com/2thumb.jpg",
                "video_url": "https://scontent.cdninstagram.com/2.mp4",
                "video_duration": 5.4,
            },
            {
                "is_video": False,
                "display_url": "https://scontent.cdninstagram.com/3.jpg",
                "video_url": None,
                "video_duration": None,
            },
        ],
    }
    view = InstaloaderPostView.model_validate(payload)
    client = _FakeClient(view=view)
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/p/SIDE2/"
    result = adapter.fetch_post(url)

    assert isinstance(result, RawPost)
    assert [asset.kind for asset in result.media] == [
        "image",
        "video",
        "thumbnail",
        "image",
    ]
    # video asset carries duration and video URL
    assert result.media[1].url == "https://scontent.cdninstagram.com/2.mp4"
    assert result.media[1].duration_seconds == 5.4
    # sibling thumbnail carries the display URL
    assert result.media[2].url == "https://scontent.cdninstagram.com/2thumb.jpg"
    assert result.media[2].duration_seconds is None


def test_fetch_post_video_returns_video_then_thumbnail_media() -> None:
    payload: dict[str, Any] = {
        "shortcode": "REEL42",
        "typename": "GraphVideo",
        "caption": "a reel",
        "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        "owner_username": "test_venue",
        "url": "https://scontent.cdninstagram.com/thumb.jpg",
        "video_url": "https://scontent.cdninstagram.com/v.mp4",
        "video_duration": 12.4,
        "mediacount": 1,
        "sidecar_nodes": [],
    }
    view = InstaloaderPostView.model_validate(payload)
    client = _FakeClient(view=view)
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/reel/REEL42/"
    result = adapter.fetch_post(url)

    assert isinstance(result, RawPost)
    assert len(result.media) == 2
    assert result.media[0].kind == "video"
    assert result.media[0].url == "https://scontent.cdninstagram.com/v.mp4"
    assert result.media[0].duration_seconds == 12.4
    assert result.media[1].kind == "thumbnail"
    assert result.media[1].url == "https://scontent.cdninstagram.com/thumb.jpg"


def test_fetch_post_returns_unsupported_media_when_typename_is_unknown() -> None:
    """An unrecognised `typename` routes to `unsupported_media` with the value named.

    The Pydantic boundary lets unknown typenames through so a schema drift
    on Meta's side (a hypothetical `GraphAudio`, or whatever ships next)
    surfaces as a typed adapter error at fetch time rather than a
    `ValidationError` in the client. The message names the observed value
    so debug output shows what Meta returned.
    """
    payload: dict[str, Any] = {
        "shortcode": "AUDIO1",
        "typename": "GraphAudio",
        "caption": "an audio post",
        "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        "owner_username": "test_venue",
        "url": "https://scontent.cdninstagram.com/thumb.jpg",
        "video_url": None,
        "video_duration": None,
        "mediacount": 1,
        "sidecar_nodes": [],
    }
    view = InstaloaderPostView.model_validate(payload)
    client = _FakeClient(view=view)
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/p/AUDIO1/"
    result = adapter.fetch_post(url)

    assert isinstance(result, dict)
    assert result["error_type"] == "unsupported_media"
    assert "GraphAudio" in result["message"]
    assert result["url"] == url


def test_fetch_post_video_without_video_url_returns_unsupported_media() -> None:
    payload: dict[str, Any] = {
        "shortcode": "REEL99",
        "typename": "GraphVideo",
        "caption": "a login-walled reel",
        "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        "owner_username": "test_venue",
        "url": "https://scontent.cdninstagram.com/thumb.jpg",
        "video_url": None,
        "video_duration": None,
        "mediacount": 1,
        "sidecar_nodes": [],
    }
    view = InstaloaderPostView.model_validate(payload)
    client = _FakeClient(view=view)
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    url = "https://www.instagram.com/reel/REEL99/"
    result = adapter.fetch_post(url)

    assert isinstance(result, dict)
    assert result["error_type"] == "unsupported_media"
    assert "video url not resolvable" in result["message"]
    assert result["url"] == url


@pytest.mark.parametrize(
    ("exception_class", "expected_error_type"),
    [
        (QueryReturnedNotFoundException, "not_found"),
        (TooManyRequestsException, "rate_limited"),
        (LoginRequiredException, "auth_failed"),
    ],
)
def test_fetch_post_maps_instaloader_exception_to_typed_error(
    exception_class: type[BaseException],
    expected_error_type: ErrorType,
) -> None:
    """Each instaloader exception surfaces as its reconciled `ErrorType`.

    The exception → ErrorType mapping is reconciled against
    `instaloader==4.15.3` and lives in `client.py`. Changing the pinned
    version updates one class name in one place; this test's parametrization
    picks it up automatically.
    """

    class _RaisingClient:
        def fetch_metadata(self, shortcode: str) -> InstaloaderPostView:
            raise InstagramClientError(expected_error_type, f"wrapped {exception_class.__name__}")

    adapter = InstagramSource(_source_config(), _RaisingClient())  # type: ignore[arg-type]

    result = adapter.fetch_post("https://www.instagram.com/p/ABC123/")

    assert isinstance(result, dict)
    assert result["error_type"] == expected_error_type


def test_targets_iterates_configured_account_urls() -> None:
    config = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[
            AccountConfig(url="https://instagram.com/venue_a"),
            AccountConfig(url="https://instagram.com/venue_b"),
        ],
    )
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(config, client)  # type: ignore[arg-type]

    assert list(adapter.targets()) == [
        "https://instagram.com/venue_a",
        "https://instagram.com/venue_b",
    ]


def test_fetch_post_accepts_reel_and_tv_url_shapes() -> None:
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(_source_config(), client)  # type: ignore[arg-type]

    # /reel/ and /tv/ shortcodes both extract cleanly; the adapter passes them
    # through to the client (which decides on typename). Since the fake client
    # returns GraphImage, the result is a happy-path RawPost — the important
    # assertion is that the URL router did not short-circuit to
    # `unsupported_source`.
    assert isinstance(adapter.fetch_post("https://www.instagram.com/reel/ABC123/"), RawPost)
    assert isinstance(adapter.fetch_post("https://instagram.com/tv/ABC123/"), RawPost)


def test_plan_for_skips_disabled_media_types_from_resolved_flags() -> None:
    """`plan_for` honours the account's resolved `MediaTypeFlags`.

    An account with `reels: false` in its resolved flags never yields a
    `("<url>", "reels")` pair — the scheduler consumes this to decide which
    fetches to run per cadence tick.
    """
    account = AccountConfig(
        url="https://instagram.com/venue_a",
        media_types=MediaTypeFlags(reels=False),
    )
    config = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[account],
    )
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(config, client)  # type: ignore[arg-type]

    plan = adapter.plan_for(account)

    assert ("https://instagram.com/venue_a", "reels") not in plan
    # the other three enabled kinds are present, in declaration order
    assert plan == [
        ("https://instagram.com/venue_a", "static_posts"),
        ("https://instagram.com/venue_a", "carousels"),
        ("https://instagram.com/venue_a", "video_posts"),
    ]


def test_plan_for_falls_back_to_source_default_media_types() -> None:
    """An account without `media_types` inherits the source-wide defaults."""
    account = AccountConfig(url="https://instagram.com/venue_b")
    config = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(video_posts=False),
        accounts=[account],
    )
    client = _FakeClient(view=_static_post_view())
    adapter = InstagramSource(config, client)  # type: ignore[arg-type]

    plan = adapter.plan_for(account)

    kinds = [kind for _, kind in plan]
    assert "video_posts" not in kinds
    assert set(kinds) == {"static_posts", "reels", "carousels"}
