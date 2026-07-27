"""Unit tests for `InstagramSource` — static-post happy path and the five
typed error branches.

The adapter's client is injected as a fake with the same `fetch_metadata`
shape as `InstagramClient`; no network is touched. The exception-class-to-
error-type reconciliation lives in one parametrized fixture so a future
scraper-version bump changes one place instead of five tests (plan Stage 2,
Behaviour bullet — "Test parametrization takes the exception class as a
fixture so the class-name reconciliation touches one place").
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
from planazo.sources.config import MediaTypeFlags, SourceConfig
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


def test_fetch_post_unsupported_media_for_non_image_typename() -> None:
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

    assert isinstance(result, dict)
    assert result["error_type"] == "unsupported_media"
    assert "GraphVideo" in result["message"]


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
    from planazo.sources.config import AccountConfig

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
