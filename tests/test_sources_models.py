from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.sources.models import MediaAsset, RawPost


def _canonical_raw_post(**overrides: object) -> RawPost:
    kwargs: dict[str, object] = {
        "source": "instagram",
        "permalink": "https://www.instagram.com/p/ABC123/",
        "title": None,
        "caption": "opening this Saturday at 20h",
        "posted_at": "2026-07-20T14:30:00+00:00",
        "author_handle": "example_venue",
        "media": [
            {
                "kind": "image",
                "url": "https://scontent.cdninstagram.com/v/example.jpg",
                "width": 1080,
                "height": 1080,
            }
        ],
    }
    kwargs.update(overrides)
    return RawPost.model_validate(kwargs)


def test_raw_post_accepts_canonical_payload_and_parses_iso8601_posted_at() -> None:
    post = _canonical_raw_post()

    assert post.source == "instagram"
    assert post.permalink == "https://www.instagram.com/p/ABC123/"
    assert post.caption == "opening this Saturday at 20h"
    assert post.posted_at == datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    assert post.author_handle == "example_venue"
    assert len(post.media) == 1
    assert post.media[0].kind == "image"


def test_raw_post_rejects_missing_permalink() -> None:
    with pytest.raises(ValidationError):
        _canonical_raw_post(permalink=None)


def test_media_asset_image_accepts_none_duration_seconds() -> None:
    asset = MediaAsset(
        kind="image",
        url="https://example.com/img.jpg",
        duration_seconds=None,
    )

    assert asset.kind == "image"
    assert asset.duration_seconds is None


def test_media_asset_video_accepts_duration_seconds() -> None:
    asset = MediaAsset(
        kind="video",
        url="https://example.com/vid.mp4",
        duration_seconds=12.4,
    )

    assert asset.kind == "video"
    assert asset.duration_seconds == 12.4


def test_media_asset_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        MediaAsset.model_validate({"kind": "gif", "url": "https://example.com/animated.gif"})
