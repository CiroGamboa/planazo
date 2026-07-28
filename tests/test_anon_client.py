"""Unit tests for `AnonInstagramClient` — parsing + exception mapping + smoke.

Tests inject a fake `http_get` that returns a stub object with `.status_code`,
`.text`, and `.json()`. The default `curl_cffi`-backed http_get is exercised
only by a light import-and-construct smoke test — no live Meta calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from planazo.sources.instagram.anon_client import (
    AnonInstagramClient,
    AnonInstagramClientError,
)


class _StubResponse:
    """Minimal `_HttpResponse` shim — `status_code`, `text`, `.json()`."""

    def __init__(self, *, status_code: int, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def _stub_client(*, status_code: int, body: Any = None, text: str = "") -> AnonInstagramClient:
    """Build an AnonInstagramClient whose one HTTP call returns the given response."""

    def http_get(_url: str, _headers: dict[str, str]) -> _StubResponse:
        return _StubResponse(status_code=status_code, body=body, text=text)

    return AnonInstagramClient(http_get=http_get)


def _twelve_edges() -> list[dict[str, Any]]:
    return [{"node": {"shortcode": f"CODE{i}"}} for i in range(12)]


def _happy_body(edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "data": {
            "user": {
                "edge_owner_to_timeline_media": {"edges": edges or _twelve_edges()},
            }
        }
    }


# ── Username extraction ───────────────────────────────────────────────────


def test_extract_username_happy_path() -> None:
    assert (
        AnonInstagramClient._extract_username("https://instagram.com/sala_apolo/") == "sala_apolo"
    )


def test_extract_username_with_www_and_query() -> None:
    assert (
        AnonInstagramClient._extract_username("https://www.instagram.com/curated.agenda/?hl=es-la")
        == "curated.agenda"
    )


def test_extract_username_no_trailing_slash() -> None:
    assert AnonInstagramClient._extract_username("https://instagram.com/bcn.agenda") == "bcn.agenda"


def test_extract_username_rejects_post_url() -> None:
    with pytest.raises(AnonInstagramClientError) as exc_info:
        AnonInstagramClient._extract_username("https://instagram.com/p/DbLvTRzIB2Y/")
    assert exc_info.value.error_type == "not_found"


def test_extract_username_rejects_reel_url() -> None:
    with pytest.raises(AnonInstagramClientError) as exc_info:
        AnonInstagramClient._extract_username("https://instagram.com/reel/ABC123/")
    assert exc_info.value.error_type == "not_found"


def test_extract_username_rejects_garbage() -> None:
    with pytest.raises(AnonInstagramClientError) as exc_info:
        AnonInstagramClient._extract_username("not-a-url")
    assert exc_info.value.error_type == "not_found"


# ── Response-shape parsing ────────────────────────────────────────────────


def test_list_recent_posts_parses_edge_owner_to_timeline_media() -> None:
    """Canned JSON response → 12 canonical shortcode URLs in edge order."""
    client = _stub_client(status_code=200, body=_happy_body())
    urls = client.list_recent_posts("https://instagram.com/bcn.agenda/", limit=12)
    assert urls == [f"https://www.instagram.com/p/CODE{i}/" for i in range(12)]


def test_list_recent_posts_honours_limit() -> None:
    client = _stub_client(status_code=200, body=_happy_body())
    urls = client.list_recent_posts("https://instagram.com/x/", limit=3)
    assert urls == [
        "https://www.instagram.com/p/CODE0/",
        "https://www.instagram.com/p/CODE1/",
        "https://www.instagram.com/p/CODE2/",
    ]


def test_list_recent_posts_falls_back_to_node_code_when_shortcode_missing() -> None:
    """Schema-drift robustness — accept `.code` when `.shortcode` is absent."""
    edges = [
        {"node": {"shortcode": "PRIMARY"}},
        {"node": {"code": "DRIFTED"}},
    ]
    client = _stub_client(status_code=200, body=_happy_body(edges))
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == [
        "https://www.instagram.com/p/PRIMARY/",
        "https://www.instagram.com/p/DRIFTED/",
    ]


# ── Exception mapping ─────────────────────────────────────────────────────


def test_400_laser_provider_maps_to_unsupported_media() -> None:
    client = _stub_client(
        status_code=400,
        text='{"message":"laser.provider blocked","error_type":"laser.provider"}',
    )
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/sala_apolo/")
    assert exc_info.value.error_type == "unsupported_media"
    assert "hikerapi" in str(exc_info.value)


def test_400_ig_business_category_subvertical_maps_to_unsupported_media() -> None:
    client = _stub_client(
        status_code=400,
        text='{"ig_business_category_subvertical":"restaurant"}',
    )
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/some_biz/")
    assert exc_info.value.error_type == "unsupported_media"


def test_400_without_business_marker_maps_to_rate_limited() -> None:
    """Generic 400 (not the business-account block) surfaces as transient."""
    client = _stub_client(status_code=400, text="generic bad request body")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_401_maps_to_auth_failed() -> None:
    client = _stub_client(status_code=401, text="unauthorized")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "auth_failed"


def test_404_maps_to_not_found() -> None:
    client = _stub_client(status_code=404, text="not found")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


def test_429_maps_to_rate_limited() -> None:
    client = _stub_client(status_code=429, text="rate limited")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_500_maps_to_rate_limited() -> None:
    client = _stub_client(status_code=500, text="server error")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_502_maps_to_rate_limited() -> None:
    client = _stub_client(status_code=502, text="bad gateway")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_network_error_maps_to_rate_limited() -> None:
    def http_get(_url: str, _headers: dict[str, str]) -> _StubResponse:
        raise RuntimeError("connection reset by peer")

    client = AnonInstagramClient(http_get=http_get)
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_unexpected_status_maps_to_rate_limited() -> None:
    """Any status outside {200, 400, 401, 404, 429, 5xx} maps conservatively."""
    client = _stub_client(status_code=418, text="teapot")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_missing_edge_owner_path_maps_to_not_found() -> None:
    """Schema-drift signal: no edges list → typed error, not IndexError/KeyError."""
    client = _stub_client(status_code=200, body={"data": {"user": {"schema_changed": True}}})
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"
    assert "edge_owner_to_timeline_media" in str(exc_info.value)


def test_top_level_shape_change_maps_to_not_found() -> None:
    """No `data` key at all → typed error."""
    client = _stub_client(status_code=200, body={"top_level_rotated": True})
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


def test_non_json_response_maps_to_not_found() -> None:
    """Body that fails to decode as JSON → schema-drift signal."""
    client = _stub_client(status_code=200, body=None, text="<html>")
    with pytest.raises(AnonInstagramClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


# ── Default session smoke ────────────────────────────────────────────────


def test_default_session_uses_curl_cffi_chrome_impersonate() -> None:
    """Construct with the default http_get — verifies curl_cffi is importable.

    No live network call — the closure is built but never invoked.
    """
    client = AnonInstagramClient()
    assert client._http_get is not None
