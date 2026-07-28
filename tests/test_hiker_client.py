"""Unit tests for `HikerClient` — spike-level coverage of parsing + error mapping.

No live HikerAPI calls; every test injects an `httpx.MockTransport` that
answers deterministically. Full test-suite coverage happens with the M3.5
scheduler integration ticket; this file locks the shape the spike ships with
so the validation script (below) exercises the same code paths a live-call
run would.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from planazo.sources.instagram.hiker_client import (
    HikerClient,
    HikerClientError,
    _shortcode_to_url,
)


def _client_with_responses(responses: dict[str, dict[str, Any]]) -> HikerClient:
    """Build a HikerClient whose `.get(path, params=...)` returns canned responses.

    `responses` maps URL path (e.g. `/v2/user/by/username`) to a dict with
    `status` (int) and `body` (dict or list — will be JSON-serialised).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        canned = responses.get(request.url.path)
        if canned is None:
            return httpx.Response(500, json={"error": f"no canned response for {request.url.path}"})
        return httpx.Response(canned["status"], json=canned["body"])

    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://api.hikerapi.com", transport=transport)
    return HikerClient(api_key="test-key", http_client=http)


# ── Construction ──────────────────────────────────────────────────────────


def test_construct_without_api_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PLANAZO_IG_HIKER_API_KEY"):
        HikerClient()


def test_construct_with_env_var_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "env-key-value")
    client = HikerClient()
    assert client._api_key == "env-key-value"


def test_construct_with_explicit_api_key_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "env-key")
    client = HikerClient(api_key="explicit-key")
    assert client._api_key == "explicit-key"


# ── Username extraction from account URL ──────────────────────────────────


def test_extract_username_happy_path() -> None:
    assert HikerClient._extract_username("https://instagram.com/sala_apolo/") == "sala_apolo"


def test_extract_username_with_www_and_query() -> None:
    assert (
        HikerClient._extract_username("https://www.instagram.com/curated.agenda/?hl=es-la")
        == "curated.agenda"
    )


def test_extract_username_no_trailing_slash() -> None:
    assert HikerClient._extract_username("https://instagram.com/bcn.agenda") == "bcn.agenda"


def test_extract_username_rejects_post_url() -> None:
    with pytest.raises(HikerClientError) as exc_info:
        HikerClient._extract_username("https://instagram.com/p/DbLvTRzIB2Y/")
    assert exc_info.value.error_type == "not_found"


def test_extract_username_rejects_reel_url() -> None:
    with pytest.raises(HikerClientError) as exc_info:
        HikerClient._extract_username("https://instagram.com/reel/ABC123/")
    assert exc_info.value.error_type == "not_found"


def test_extract_username_rejects_garbage() -> None:
    with pytest.raises(HikerClientError) as exc_info:
        HikerClient._extract_username("not-a-url")
    assert exc_info.value.error_type == "not_found"


# ── Shortcode → URL helper ────────────────────────────────────────────────


def test_shortcode_to_url_canonical_shape() -> None:
    assert _shortcode_to_url("DbLvTRzIB2Y") == "https://www.instagram.com/p/DbLvTRzIB2Y/"


# ── HTTP error mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected_error_type",
    [
        (401, "auth_failed"),
        (403, "auth_failed"),
        (404, "not_found"),
        (422, "not_found"),
        (429, "rate_limited"),
        (500, "rate_limited"),
        (502, "rate_limited"),
        (503, "rate_limited"),
        (418, "rate_limited"),  # unknown status → rate_limited (conservative)
    ],
)
def test_http_status_maps_to_error_type(status: int, expected_error_type: str) -> None:
    client = _client_with_responses(
        {"/v2/user/by/username": {"status": status, "body": {"detail": "err"}}}
    )
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/anyaccount/")
    assert exc_info.value.error_type == expected_error_type


def test_network_error_maps_to_rate_limited() -> None:
    def raiser(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(base_url="https://api.hikerapi.com", transport=httpx.MockTransport(raiser))
    client = HikerClient(api_key="test-key", http_client=http)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_timeout_maps_to_rate_limited() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    http = httpx.Client(base_url="https://api.hikerapi.com", transport=httpx.MockTransport(timeout))
    client = HikerClient(api_key="test-key", http_client=http)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


# ── Response-shape parsing ────────────────────────────────────────────────


def test_list_recent_posts_happy_path_returns_canonical_urls() -> None:
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "789456", "pk": "789456"}},
            "/g2/user/medias": {
                "status": 200,
                "body": {
                    "response": {
                        "items": [
                            {"code": "Da5QWjLo8jn", "pk": 1, "like_count": 100},
                            {"code": "DbLvTRzIB2Y", "pk": 2, "like_count": 50},
                            {"code": "DcXyZaBcDeF", "pk": 3, "like_count": 25},
                        ],
                        "more_available": True,
                    },
                    "next_page_id": "cursor_abc",
                },
            },
        }
    )
    urls = client.list_recent_posts("https://instagram.com/bcn.agenda/", limit=12)
    assert urls == [
        "https://www.instagram.com/p/Da5QWjLo8jn/",
        "https://www.instagram.com/p/DbLvTRzIB2Y/",
        "https://www.instagram.com/p/DcXyZaBcDeF/",
    ]


def test_list_recent_posts_honours_limit() -> None:
    items = [{"code": f"CODE{i}"} for i in range(20)]
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "111"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": items}}},
        }
    )
    urls = client.list_recent_posts("https://instagram.com/x/", limit=5)
    assert len(urls) == 5
    assert urls[0] == "https://www.instagram.com/p/CODE0/"
    assert urls[-1] == "https://www.instagram.com/p/CODE4/"


def test_list_recent_posts_skips_items_without_code() -> None:
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "111"}},
            "/g2/user/medias": {
                "status": 200,
                "body": {
                    "response": {"items": [{"code": "AAA"}, {"no_code": "x"}, {"code": "BBB"}]}
                },
            },
        }
    )
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == [
        "https://www.instagram.com/p/AAA/",
        "https://www.instagram.com/p/BBB/",
    ]


def test_user_by_username_missing_id_field_maps_to_not_found() -> None:
    client = _client_with_responses(
        {"/v2/user/by/username": {"status": 200, "body": {"unexpected": "shape"}}}
    )
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"
    assert "no id field" in str(exc_info.value)


def test_user_by_username_wrapped_in_user_key_still_works() -> None:
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"user": {"pk": "42"}}},
            "/g2/user/medias": {
                "status": 200,
                "body": {"response": {"items": [{"code": "XXX"}]}},
            },
        }
    )
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == ["https://www.instagram.com/p/XXX/"]


def test_user_medias_flat_items_shape_also_supported() -> None:
    # Fallback in case HikerAPI returns `items[]` at top level (no `response` envelope)
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"items": [{"code": "TOP"}]}},
        }
    )
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == ["https://www.instagram.com/p/TOP/"]


def test_user_medias_missing_items_maps_to_not_found() -> None:
    client = _client_with_responses(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"more_available": False}}},
        }
    )
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


def test_non_json_response_maps_to_not_found() -> None:
    def html_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    http = httpx.Client(
        base_url="https://api.hikerapi.com", transport=httpx.MockTransport(html_body)
    )
    client = HikerClient(api_key="test-key", http_client=http)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"
