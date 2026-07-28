"""Unit tests for `build_fetch_instagram_post` — the LLM-facing wrapper around
`InstagramSource.fetch_post`.

Covers three axes: happy-path serialization (RawPost → JSON-safe dict), typed
error passthrough (source-adapter error dict crosses the wrapper unchanged),
and schema shape (the schema `agentlib.tools.call` sees derives from the inner
callable's signature — one `url` parameter, required, string-typed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from planazo.sources.instagram.tools import build_fetch_instagram_post
from planazo.sources.models import MediaAsset, RawPost


class _FakeSource:
    """Fake `InstagramSource` — returns one canned result per instance."""

    def __init__(self, result: RawPost | dict[str, Any]) -> None:
        self._result = result
        self.calls: list[str] = []

    def fetch_post(self, url: str) -> RawPost | dict[str, Any]:
        self.calls.append(url)
        return self._result


def _static_post() -> RawPost:
    return RawPost(
        source="instagram",
        permalink="https://www.instagram.com/p/ABC123/",
        title=None,
        caption="hello barcelona",
        posted_at=datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        author_handle="test_venue",
        media=[MediaAsset(kind="image", url="https://scontent.cdninstagram.com/i.jpg")],
    )


def test_fetch_instagram_post_returns_model_dump_on_raw_post() -> None:
    post = _static_post()
    fake = _FakeSource(post)
    _, fetch = build_fetch_instagram_post(fake)  # type: ignore[arg-type]

    result = fetch(url="https://www.instagram.com/p/ABC123/")

    assert result == post.model_dump(mode="json")
    assert fake.calls == ["https://www.instagram.com/p/ABC123/"]


def test_fetch_instagram_post_passes_through_error_dict_unchanged() -> None:
    error: dict[str, Any] = {
        "error_type": "not_found",
        "message": "post gone",
        "url": "https://www.instagram.com/p/MISSING/",
    }
    fake = _FakeSource(error)
    _, fetch = build_fetch_instagram_post(fake)  # type: ignore[arg-type]

    result = fetch(url="https://www.instagram.com/p/MISSING/")

    assert result == error


def test_build_fetch_instagram_post_schema_shape() -> None:
    fake = _FakeSource(_static_post())
    schema, _ = build_fetch_instagram_post(fake)  # type: ignore[arg-type]

    assert schema["name"] == "fetch_instagram_post"
    assert schema["type"] == "function"
    assert schema["description"]  # non-empty — schema_for pulls from docstring
    parameters = schema["parameters"]
    assert parameters["required"] == ["url"]
    assert parameters["properties"]["url"]["type"] == "string"
    # `source` is captured by closure — the LLM must not see it.
    assert "source" not in parameters["properties"]


def test_build_fetch_instagram_post_closes_over_source_and_hides_it() -> None:
    # Two distinct sources → two distinct wrappers routing to their own source.
    error_a: dict[str, Any] = {"error_type": "rate_limited", "message": "a", "url": "a"}
    error_b: dict[str, Any] = {"error_type": "not_found", "message": "b", "url": "b"}
    fake_a = _FakeSource(error_a)
    fake_b = _FakeSource(error_b)

    _, fetch_a = build_fetch_instagram_post(fake_a)  # type: ignore[arg-type]
    _, fetch_b = build_fetch_instagram_post(fake_b)  # type: ignore[arg-type]

    assert fetch_a(url="a") == error_a
    assert fetch_b(url="b") == error_b
    assert fake_a.calls == ["a"]
    assert fake_b.calls == ["b"]
