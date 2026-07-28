"""Unit tests for `HikerClient` — pool + retirement + parsing + error mapping.

Tests inject `httpx.MockTransport`-backed `httpx.Client` factories so every
HTTP round-trip is answered deterministically. Pool-behavior tests seed the
RNG at file scope — a future maintainer who removes the seed will discover
the deterministic-draw assertions were load-bearing.
"""

from __future__ import annotations

import contextlib
import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from planazo.sources.instagram.hiker_client import (
    RETIREMENT_WINDOW,
    HikerClient,
    HikerClientError,
    _read_key_pool,
    _shortcode_to_url,
)

# File-scope seed — pool-behavior tests rely on deterministic `random.choice`
# draws so the "at least 2 distinct keys in 3 draws" assertion locks the
# random-selection contract. Do not remove.
random.seed(0)


def _factory_for(
    responses: dict[str, dict[str, Any]],
) -> tuple[
    list[tuple[str, str]],
    Any,
]:
    """Build an `http_client_factory` that answers `responses` deterministically.

    Returns a `(observed, factory)` tuple where `observed` collects one
    `(api_key, path)` entry per request so tests can assert on which key
    served each call. `responses` maps URL path (e.g. `/v2/user/by/username`)
    to a dict with `status` (int) and `body` (dict — JSON-serialised).
    """
    observed: list[tuple[str, str]] = []

    def factory(api_key: str) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            observed.append((api_key, request.url.path))
            canned = responses.get(request.url.path)
            if canned is None:
                return httpx.Response(
                    500, json={"error": f"no canned response for {request.url.path}"}
                )
            return httpx.Response(canned["status"], json=canned["body"])

        return httpx.Client(
            base_url="https://api.hikerapi.com",
            transport=httpx.MockTransport(handler),
            headers={"x-access-key": api_key},
        )

    return observed, factory


def _factory_per_key(
    per_key_responses: dict[str, dict[str, dict[str, Any]]],
) -> tuple[list[tuple[str, str]], Any]:
    """Build a factory where each key sees a different response for the same path.

    `per_key_responses[api_key][path] = {"status": int, "body": dict}`.
    """
    observed: list[tuple[str, str]] = []

    def factory(api_key: str) -> httpx.Client:
        canned_for_key = per_key_responses.get(api_key, {})

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append((api_key, request.url.path))
            canned = canned_for_key.get(request.url.path)
            if canned is None:
                return httpx.Response(
                    500, json={"error": f"no canned response for {api_key}:{request.url.path}"}
                )
            return httpx.Response(canned["status"], json=canned["body"])

        return httpx.Client(
            base_url="https://api.hikerapi.com",
            transport=httpx.MockTransport(handler),
            headers={"x-access-key": api_key},
        )

    return observed, factory


def _single_key_client(api_key: str = "test-key") -> HikerClient:
    """Build a HikerClient with a single key and a 200 OK factory for every path.

    Used by parsing tests that don't care about pool behavior.
    """

    def factory(_key: str) -> httpx.Client:
        return httpx.Client(base_url="https://api.hikerapi.com")

    return HikerClient(api_keys=[api_key], http_client_factory=factory)


# ── Construction — api_keys + from_env ─────────────────────────────────────


def test_construct_with_explicit_api_keys_uses_them() -> None:
    client = HikerClient(api_keys=["key-a", "key-b"])
    assert client._keys == ["key-a", "key-b"]


def test_construct_with_empty_api_keys_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PLANAZO_IG_HIKER_API_KEY"):
        HikerClient(api_keys=[])


def test_construct_with_none_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "env-only-key")
    client = HikerClient()
    assert client._keys == ["env-only-key"]


def test_construct_deduplicates_repeated_values_in_api_keys() -> None:
    client = HikerClient(api_keys=["shared", "shared", "unique"])
    assert client._keys == ["shared", "unique"]


def test_construction_initializes_all_keys_as_available() -> None:
    client = HikerClient(api_keys=["k1", "k2", "k3"])
    assert client._retired_until == {"k1": None, "k2": None, "k3": None}


def test_from_env_reads_singular_key_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "singular-key")
    client = HikerClient.from_env()
    assert client._keys == ["singular-key"]


def test_from_env_reads_numbered_pool_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_1", "num-1")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_2", "num-2")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_3", "num-3")
    client = HikerClient.from_env()
    assert sorted(client._keys) == ["num-1", "num-2", "num-3"]


def test_from_env_reads_singular_and_numbered_pool_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Singular is NOT a fallback — it's an additive peer entry in the pool."""
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "singular-secret")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_1", "num-1-secret")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_2", "num-2-secret")
    client = HikerClient.from_env()
    assert sorted(client._keys) == ["num-1-secret", "num-2-secret", "singular-secret"]


def test_from_env_dedupes_identical_key_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same secret under two env-var names → single pool entry."""
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "same-secret")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_1", "same-secret")
    client = HikerClient.from_env()
    assert client._keys == ["same-secret"]


def test_from_env_with_no_keys_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="no HikerAPI keys available"):
        HikerClient.from_env()


def _numbered_env_names() -> list[str]:
    """Names any test in this file might set — clear them defensively."""
    return [f"PLANAZO_IG_HIKER_API_KEY_{i}" for i in range(1, 10)]


def test_read_key_pool_ignores_empty_and_whitespace_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLANAZO_IG_HIKER_API_KEY", raising=False)
    for name in _numbered_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY", "   ")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_1", "")
    monkeypatch.setenv("PLANAZO_IG_HIKER_API_KEY_2", "real-key")
    assert _read_key_pool() == ["real-key"]


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


# ── HTTP error mapping — non-retirable statuses ───────────────────────────


@pytest.mark.parametrize(
    "status,expected_error_type",
    [
        (404, "not_found"),
        (422, "not_found"),
        (500, "rate_limited"),
        (502, "rate_limited"),
        (503, "rate_limited"),
        (418, "rate_limited"),  # unknown status → rate_limited (conservative)
    ],
)
def test_non_retirable_http_status_maps_to_error_type(
    status: int, expected_error_type: str
) -> None:
    _observed, factory = _factory_for(
        {"/v2/user/by/username": {"status": status, "body": {"detail": "err"}}}
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/anyaccount/")
    assert exc_info.value.error_type == expected_error_type


def test_network_error_maps_to_rate_limited() -> None:
    def factory(_key: str) -> httpx.Client:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return httpx.Client(
            base_url="https://api.hikerapi.com", transport=httpx.MockTransport(handler)
        )

    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


def test_timeout_maps_to_rate_limited() -> None:
    def factory(_key: str) -> httpx.Client:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        return httpx.Client(
            base_url="https://api.hikerapi.com", transport=httpx.MockTransport(handler)
        )

    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"


# ── Response-shape parsing (single-key pool, always-200 factory) ──────────


def test_list_recent_posts_happy_path_returns_canonical_urls() -> None:
    _observed, factory = _factory_for(
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
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    urls = client.list_recent_posts("https://instagram.com/bcn.agenda/", limit=12)
    assert urls == [
        "https://www.instagram.com/p/Da5QWjLo8jn/",
        "https://www.instagram.com/p/DbLvTRzIB2Y/",
        "https://www.instagram.com/p/DcXyZaBcDeF/",
    ]


def test_list_recent_posts_honours_limit() -> None:
    items = [{"code": f"CODE{i}"} for i in range(20)]
    _observed, factory = _factory_for(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "111"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": items}}},
        }
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    urls = client.list_recent_posts("https://instagram.com/x/", limit=5)
    assert len(urls) == 5
    assert urls[0] == "https://www.instagram.com/p/CODE0/"
    assert urls[-1] == "https://www.instagram.com/p/CODE4/"


def test_list_recent_posts_skips_items_without_code() -> None:
    _observed, factory = _factory_for(
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
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == [
        "https://www.instagram.com/p/AAA/",
        "https://www.instagram.com/p/BBB/",
    ]


def test_user_by_username_missing_id_field_maps_to_not_found() -> None:
    _observed, factory = _factory_for(
        {"/v2/user/by/username": {"status": 200, "body": {"unexpected": "shape"}}}
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"
    assert "no id field" in str(exc_info.value)


def test_user_by_username_wrapped_in_user_key_still_works() -> None:
    _observed, factory = _factory_for(
        {
            "/v2/user/by/username": {"status": 200, "body": {"user": {"pk": "42"}}},
            "/g2/user/medias": {
                "status": 200,
                "body": {"response": {"items": [{"code": "XXX"}]}},
            },
        }
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == ["https://www.instagram.com/p/XXX/"]


def test_user_medias_flat_items_shape_also_supported() -> None:
    _observed, factory = _factory_for(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"items": [{"code": "TOP"}]}},
        }
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    urls = client.list_recent_posts("https://instagram.com/x/")
    assert urls == ["https://www.instagram.com/p/TOP/"]


def test_user_medias_missing_items_maps_to_not_found() -> None:
    _observed, factory = _factory_for(
        {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"more_available": False}}},
        }
    )
    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


def test_non_json_response_maps_to_not_found() -> None:
    def factory(_key: str) -> httpx.Client:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        return httpx.Client(
            base_url="https://api.hikerapi.com", transport=httpx.MockTransport(handler)
        )

    client = HikerClient(api_keys=["k1"], http_client_factory=factory)
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "not_found"


# ── Multi-key pool behavior ───────────────────────────────────────────────


def test_three_successful_requests_visit_multiple_keys_with_seeded_rng() -> None:
    """3-key pool, all 200 OK. Seeded RNG → multiple distinct keys hit.

    File-scope seed(0) makes `random.choice` over ["k1","k2","k3"] hit at
    least two distinct keys across the six requests three `list_recent_posts`
    calls make (two calls each: username→user_id, user_id→medias).
    """
    random.seed(0)
    per_key = {
        k: {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "X"}]}}},
        }
        for k in ("k1", "k2", "k3")
    }
    observed, factory = _factory_per_key(per_key)
    client = HikerClient(api_keys=["k1", "k2", "k3"], http_client_factory=factory)
    for _ in range(3):
        client.list_recent_posts("https://instagram.com/x/")
    keys_used = {api_key for api_key, _path in observed}
    assert len(keys_used) >= 2, f"expected >=2 distinct keys, got {keys_used}"


def test_429_retires_key_and_next_draw_only_samples_remaining_keys() -> None:
    """Retired key never appears in a subsequent request's header."""
    random.seed(0)
    per_key = {
        "k_bad": {
            "/v2/user/by/username": {"status": 429, "body": {"detail": "rate limit"}},
            "/g2/user/medias": {"status": 429, "body": {"detail": "rate limit"}},
        },
        "k_ok_a": {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "A"}]}}},
        },
        "k_ok_b": {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "B"}]}}},
        },
    }
    observed, factory = _factory_per_key(per_key)
    client = HikerClient(api_keys=["k_bad", "k_ok_a", "k_ok_b"], http_client_factory=factory)
    # Retire k_bad by forcing a call that hits it — repeat until we've seen it
    # retire (deterministic under seed but keep the intent explicit).
    for _ in range(6):
        with contextlib.suppress(HikerClientError):
            client.list_recent_posts("https://instagram.com/x/")
        if client._retired_until.get("k_bad") is not None:
            break
    # From here on, k_bad must NOT appear in any request header.
    observed.clear()
    for _ in range(4):
        client.list_recent_posts("https://instagram.com/x/")
    keys_used = {api_key for api_key, _path in observed}
    assert "k_bad" not in keys_used
    assert keys_used.issubset({"k_ok_a", "k_ok_b"})


def test_401_retires_key_for_full_window() -> None:
    """401 on key A rotates to key B; A ends up retired for RETIREMENT_WINDOW."""
    random.seed(0)
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    per_key = {
        "k_a": {
            "/v2/user/by/username": {"status": 401, "body": {"detail": "unauthorized"}},
            "/g2/user/medias": {"status": 401, "body": {"detail": "unauthorized"}},
        },
        "k_b": {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "Z"}]}}},
        },
    }
    _observed, factory = _factory_per_key(per_key)
    client = HikerClient(
        api_keys=["k_a", "k_b"], http_client_factory=factory, now=lambda: fixed_now
    )
    # Force enough draws that k_a is hit at least once by the seeded random.
    for _ in range(6):
        with contextlib.suppress(HikerClientError):
            client.list_recent_posts("https://instagram.com/x/")
        if client._retired_until.get("k_a") is not None:
            break
    assert client._retired_until["k_a"] == fixed_now + RETIREMENT_WINDOW


def test_403_retires_key_for_full_window() -> None:
    random.seed(0)
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    per_key = {
        "k_a": {
            "/v2/user/by/username": {"status": 403, "body": {"detail": "quota"}},
            "/g2/user/medias": {"status": 403, "body": {"detail": "quota"}},
        },
        "k_b": {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "Z"}]}}},
        },
    }
    _observed, factory = _factory_per_key(per_key)
    client = HikerClient(
        api_keys=["k_a", "k_b"], http_client_factory=factory, now=lambda: fixed_now
    )
    for _ in range(6):
        with contextlib.suppress(HikerClientError):
            client.list_recent_posts("https://instagram.com/x/")
        if client._retired_until.get("k_a") is not None:
            break
    assert client._retired_until["k_a"] == fixed_now + RETIREMENT_WINDOW


def test_all_keys_429_surfaces_rate_limited_with_next_available_timestamp() -> None:
    """Every key 429s → HikerClientError('rate_limited', 'all 3 ... next available at ...')."""
    random.seed(0)
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    per_key = {
        k: {
            "/v2/user/by/username": {"status": 429, "body": {"detail": "rate limit"}},
            "/g2/user/medias": {"status": 429, "body": {"detail": "rate limit"}},
        }
        for k in ("k1", "k2", "k3")
    }
    _observed, factory = _factory_per_key(per_key)
    client = HikerClient(
        api_keys=["k1", "k2", "k3"], http_client_factory=factory, now=lambda: fixed_now
    )
    with pytest.raises(HikerClientError) as exc_info:
        client.list_recent_posts("https://instagram.com/x/")
    assert exc_info.value.error_type == "rate_limited"
    message = str(exc_info.value)
    assert "all 3 hikerapi keys retired" in message
    expected_iso = (fixed_now + RETIREMENT_WINDOW).isoformat()
    assert expected_iso in message


def test_retired_key_re_enters_pool_after_window_elapses() -> None:
    """Advance the injected clock past RETIREMENT_WINDOW → retired key becomes available."""
    random.seed(0)
    clock_state = {"now": datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)}

    def now() -> datetime:
        return clock_state["now"]

    per_key = {
        "k_a": {
            "/v2/user/by/username": {"status": 429, "body": {"detail": "rate limit"}},
            "/g2/user/medias": {"status": 429, "body": {"detail": "rate limit"}},
        },
        "k_b": {
            "/v2/user/by/username": {"status": 200, "body": {"id": "1"}},
            "/g2/user/medias": {"status": 200, "body": {"response": {"items": [{"code": "Z"}]}}},
        },
    }
    _observed, factory = _factory_per_key(per_key)
    client = HikerClient(api_keys=["k_a", "k_b"], http_client_factory=factory, now=now)
    # Force k_a into retirement.
    for _ in range(6):
        with contextlib.suppress(HikerClientError):
            client.list_recent_posts("https://instagram.com/x/")
        if client._retired_until.get("k_a") is not None:
            break
    assert client._retired_until["k_a"] is not None
    # Advance past the retirement window.
    clock_state["now"] = clock_state["now"] + RETIREMENT_WINDOW + timedelta(seconds=1)
    # A fresh draw should re-admit k_a: _pick_available_key clears expired
    # retirements on read.
    _ = client._pick_available_key()
    assert client._retired_until["k_a"] is None


def test_retirement_log_line_masks_full_key_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning log line contains only the 6-char key prefix + '…', never the full secret."""
    random.seed(0)
    secret = "very-secret-key-value-do-not-leak"
    _observed, factory = _factory_for(
        {"/v2/user/by/username": {"status": 429, "body": {"detail": "rate limit"}}}
    )
    client = HikerClient(api_keys=[secret], http_client_factory=factory)
    with (
        caplog.at_level(logging.WARNING, logger="planazo.sources.instagram.hiker_client"),
        pytest.raises(HikerClientError),
    ):
        client.list_recent_posts("https://instagram.com/x/")
    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warning_records, "expected at least one WARNING log record on retirement"
    for record in warning_records:
        message = record.getMessage()
        assert secret not in message
        assert secret[:6] in message
