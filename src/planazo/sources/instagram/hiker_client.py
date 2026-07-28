"""HikerAPI-backed Instagram discovery client with a multi-key pool.

Purpose (M3.5): given an Instagram account URL, return the last N post URLs
so the scheduler (#67) can iterate them for business venue accounts the
anonymous `web_profile_info` backend refuses with a `laser.provider` block.
Extraction of each returned post URL is still handled by the existing
`InstagramClient` + `instaloader.Post.from_shortcode` — this client's ONLY
job is discovery.

Multi-key pool with random selection + retirement window
--------------------------------------------------------

Every request draws one key uniformly at random from the pool of NON-retired
keys via `random.choice`. When a request returns 401, 403, or 429, the drawing
key is retired for `RETIREMENT_WINDOW` (5 minutes) and the request retries
with a fresh draw. Retries are capped at pool size — if every key retires
before the request lands, the client raises
`HikerClientError("rate_limited", "all N hikerapi keys retired; next available
at <iso-ts>")` naming the earliest expiry so the operator can decide whether
to wait or add more keys.

Random selection is a deliberate load-balancing choice — round-robin creates
predictable inter-request timing patterns Meta could fingerprint over a long
horizon, and first-viable-with-sticky concentrates traffic on one key until
it gets flagged. Uniform random distributes traffic across the pool in
expectation, and the retirement window prevents an immediate re-draw of a
just-rate-limited key.

`random.choice` (not `secrets.choice`) is the right tool: this is load-
balancing, not a security primitive. Tests seed the RNG at file scope for
determinism; the client itself never seeds.

Endpoint choices (per OpenAPI at api.hikerapi.com/openapi.json):

- `GET /v2/user/by/username?username={u}` — username → user_id lookup.
- `GET /g2/user/medias?user_id={id}` — user_id → PageResponse of media
  objects. Each item has a `.code` field (the Instagram shortcode). We build
  the canonical URL `https://www.instagram.com/p/{code}/` from the code.

Exception mapping (aligned with `client.py`'s `ErrorType` taxonomy from
`sources/base.py`):

| HTTP status / condition       | ErrorType             | Notes                                    |
|-------------------------------|-----------------------|------------------------------------------|
| 401 (invalid api key)         | retire + rotate       | All-keys-401 surfaces as rate_limited    |
| 403 (subscription / quota)    | retire + rotate       | Same rotation semantics as 401           |
| 404 (username not found)      | `not_found`           | Instagram username does not exist        |
| 422 (validation error body)   | `not_found`           | Malformed username / user_id             |
| 429 (rate limit)              | retire + rotate       | Retirement window prevents re-draw       |
| 5xx or network error          | `rate_limited`        | Transient; retry later                   |
| response empty / no `items[]` | `not_found`           | Meta returned no medias for this account |
| all pool keys retired         | `rate_limited`        | Message names earliest expiry            |

Env-var configuration (composition-root only — never inside the tick loop):

- `PLANAZO_IG_HIKER_API_KEY` — a single key (peer entry in the pool).
- `PLANAZO_IG_HIKER_API_KEY_1`, `PLANAZO_IG_HIKER_API_KEY_2`, ... — additional
  peer entries (any numbered suffix; discovered by glob).

Both env-var families are peers — the singular is not a fallback. Values are
de-duplicated across the pool, so the same secret set under both names counts
once. Absent both → `RuntimeError` at construction (caller wires this at the
composition root, not inside the tick loop).
"""

from __future__ import annotations

import logging
import os
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import httpx

from planazo.sources.base import ErrorType
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol

__all__ = [
    "HikerClient",
    "HikerClientError",
    "InstagramDiscoveryProtocol",
]

logger = logging.getLogger(__name__)

BASE_URL: Final[str] = "https://api.hikerapi.com"
API_KEY_HEADER: Final[str] = "x-access-key"
TIMEOUT_SECONDS: Final[float] = 15.0
RETIREMENT_WINDOW: Final[timedelta] = timedelta(minutes=5)

_ENV_KEY_SINGULAR: Final[str] = "PLANAZO_IG_HIKER_API_KEY"
_ENV_KEY_NUMBERED_PREFIX: Final[str] = "PLANAZO_IG_HIKER_API_KEY_"

_INSTAGRAM_USERNAME_URL: Final = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?P<username>[A-Za-z0-9._]+)/?(?:\?.*)?$"
)


class _HttpClientFactory(Protocol):
    def __call__(self, api_key: str) -> httpx.Client: ...


class HikerClientError(Exception):
    """Wrapper exception carrying an `ErrorType` — matches `InstagramClientError`."""

    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type: ErrorType = error_type


def _read_key_pool() -> list[str]:
    """Collect all HikerAPI keys from BOTH env-var families, de-duplicated by value.

    `PLANAZO_IG_HIKER_API_KEY` (singular) and every `PLANAZO_IG_HIKER_API_KEY_*`
    (numbered) contribute peer entries to the pool. Empty values (`""`, whitespace)
    are ignored. Same secret under two env-var names appears exactly once in
    the returned list, preserving discovery order (singular first when present,
    then numbered in name order).
    """
    seen: set[str] = set()
    pool: list[str] = []

    def _consider(value: str | None) -> None:
        if value is None:
            return
        stripped = value.strip()
        if not stripped or stripped in seen:
            return
        seen.add(stripped)
        pool.append(stripped)

    _consider(os.environ.get(_ENV_KEY_SINGULAR))
    for name in sorted(os.environ):
        if name.startswith(_ENV_KEY_NUMBERED_PREFIX) and name != _ENV_KEY_SINGULAR:
            _consider(os.environ.get(name))
    return pool


class HikerClient:
    """HikerAPI discovery client with a random-selection multi-key pool.

    Every `list_recent_posts` call may make multiple HikerAPI requests
    (username→user_id, then user_id→medias page); each request draws a key
    uniformly at random from the pool of non-retired keys. On 401/403/429 the
    drawing key retires for `RETIREMENT_WINDOW` and the request retries with
    a fresh draw. Retries are capped at pool size so a fully-retired pool
    surfaces as a typed `rate_limited` error, not an infinite loop.

    Constructor accepts an optional `http_client_factory(api_key)` for tests
    to inject fakes — production default builds an `httpx.Client` per key with
    the `x-access-key` header baked in. `now` is injected so retirement-window
    tests are deterministic.

    Absent an explicit `api_keys` list AND both env-var families empty →
    `RuntimeError`; the scheduler's composition root is responsible for
    ensuring at least one key is present when this backend is configured.
    """

    def __init__(
        self,
        *,
        api_keys: list[str] | None = None,
        http_client_factory: _HttpClientFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if api_keys is None:
            pool = _read_key_pool()
        else:
            # De-duplicate a caller-supplied list by value while preserving order.
            seen: set[str] = set()
            pool = []
            for candidate in api_keys:
                stripped = candidate.strip()
                if not stripped or stripped in seen:
                    continue
                seen.add(stripped)
                pool.append(stripped)
        if not pool:
            raise RuntimeError(
                f"no HikerAPI keys available — set {_ENV_KEY_SINGULAR} or "
                f"{_ENV_KEY_NUMBERED_PREFIX}<N> in .env, or pass api_keys= explicitly."
            )
        self._keys: list[str] = pool
        self._retired_until: dict[str, datetime | None] = {key: None for key in pool}
        self._http_client_factory: _HttpClientFactory = (
            http_client_factory if http_client_factory is not None else self._default_client_factory
        )
        self._clients: dict[str, httpx.Client] = {}
        self._now: Callable[[], datetime] = now if now is not None else _default_now

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        """Return the last `limit` post URLs for the account at `account_url`.

        Two HikerAPI calls per invocation — each may rotate keys independently
        on a 401/403/429. Result URLs are canonical
        `https://www.instagram.com/p/{code}/` (no `?hl=`, no `img_index=`) —
        the shape `extract_once` already handles.

        Raises `HikerClientError` with the mapped `ErrorType` on any documented
        failure branch. Everything else — schema drift, unknown response
        shape — surfaces uncaught so an unknown failure is visible rather
        than silently coerced (matches `InstagramClient.fetch_metadata`).
        """
        username = self._extract_username(account_url)

        user_payload = self._get("/v2/user/by/username", params={"username": username})
        user_id = self._extract_user_id(user_payload, username)

        page = self._get("/g2/user/medias", params={"user_id": user_id})
        items = self._extract_items(page)

        return [_shortcode_to_url(item["code"]) for item in items[:limit] if "code" in item]

    @classmethod
    def from_env(cls) -> HikerClient:
        """Read the pool from `PLANAZO_IG_HIKER_API_KEY[_*]` and construct.

        Both env-var families are peers — the singular is not a fallback. Value-
        based de-duplication means the same secret set under two names appears
        exactly once. Absent both → `RuntimeError`.
        """
        return cls(api_keys=_read_key_pool())

    # ── HTTP + key-pool internals ─────────────────────────────────────────

    def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        """HTTP GET with per-request random key draw + retirement-on-401/403/429.

        Loops until either the request succeeds (200) or every key in the pool
        has been retired within the current call. Retries are bounded by pool
        size — the loop exits after at most `len(self._keys)` iterations. Non-
        retirable errors (4xx not in {401, 403, 429}, 5xx, network) surface
        immediately without rotating.
        """
        tried_keys: set[str] = set()
        while True:
            key = self._pick_available_key()
            if key in tried_keys:
                # Every non-retired key has already been tried in this call and
                # a fresh draw somehow re-picked one — treat as exhausted so we
                # never spin forever.
                self._raise_all_retired()
            tried_keys.add(key)
            client = self._client_for(key)
            try:
                response = client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise HikerClientError(
                    "rate_limited", f"hikerapi timeout on {path} ({exc})"
                ) from exc
            except httpx.RequestError as exc:
                raise HikerClientError(
                    "rate_limited", f"hikerapi network error on {path} ({exc})"
                ) from exc

            status = response.status_code
            if status in (401, 403, 429):
                self._retire_key(key, f"HTTP {status}")
                # Retry with a fresh draw. If _pick_available_key finds none,
                # it raises rate_limited with the earliest-expiry timestamp.
                continue
            if status == 404:
                raise HikerClientError("not_found", f"hikerapi 404 on {path} params={params}")
            if status == 422:
                raise HikerClientError(
                    "not_found",
                    f"hikerapi validation error on {path} ({response.text[:200]!r})",
                )
            if 500 <= status < 600:
                raise HikerClientError(
                    "rate_limited", f"hikerapi server error on {path} ({status})"
                )
            if status != 200:
                raise HikerClientError(
                    "rate_limited",
                    f"hikerapi unexpected status {status} on {path}: {response.text[:200]!r}",
                )

            try:
                return response.json()
            except ValueError as exc:
                raise HikerClientError(
                    "not_found", f"hikerapi returned non-JSON body on {path} ({exc})"
                ) from exc

    def _pick_available_key(self) -> str:
        """Draw one key uniformly at random from non-retired keys, else raise.

        A key is available when `_retired_until[key]` is `None` OR its expiry
        is in the past relative to the injected `now`. Expired retirements are
        cleared on read (so a re-admitted key shows as `None` on the next
        introspection). Empty available pool → `HikerClientError("rate_limited")`
        naming the earliest expiry so the operator can decide when to retry.
        """
        current = self._now()
        available: list[str] = []
        for key, retired_until in self._retired_until.items():
            if retired_until is None:
                available.append(key)
            elif current >= retired_until:
                self._retired_until[key] = None
                available.append(key)
        if not available:
            self._raise_all_retired()
        return random.choice(available)

    def _retire_key(self, key: str, reason: str) -> None:
        """Retire `key` for `RETIREMENT_WINDOW` and log the rotation.

        The log line contains only the 6-char key prefix + `…` — never the full
        secret. Callers who need the whole key inspect `._retired_until` in
        tests.
        """
        retired_until = self._now() + RETIREMENT_WINDOW
        self._retired_until[key] = retired_until
        logger.warning(
            "hikerapi key %s… exhausted (%s); retiring until %s",
            key[:6],
            reason,
            retired_until.isoformat(),
        )

    def _raise_all_retired(self) -> None:
        """Raise `rate_limited` naming the earliest `_retired_until` timestamp."""
        expiries = [ts for ts in self._retired_until.values() if ts is not None]
        earliest = min(expiries) if expiries else self._now()
        raise HikerClientError(
            "rate_limited",
            f"all {len(self._keys)} hikerapi keys retired; "
            f"next available at {earliest.isoformat()}",
        )

    def _client_for(self, key: str) -> httpx.Client:
        """Return (and lazily cache) the `httpx.Client` for `key`."""
        client = self._clients.get(key)
        if client is None:
            client = self._http_client_factory(key)
            self._clients[key] = client
        return client

    @staticmethod
    def _default_client_factory(api_key: str) -> httpx.Client:
        return httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            headers={API_KEY_HEADER: api_key},
        )

    # ── Payload parsing ───────────────────────────────────────────────────

    @staticmethod
    def _extract_username(account_url: str) -> str:
        """Parse the username from an Instagram account URL.

        Accepts trailing slash and `?hl=xx-yy` variants; rejects post URLs
        (`/p/{shortcode}/`) and reel URLs (`/reel/{shortcode}/`) which are
        not the shape this method takes.
        """
        m = _INSTAGRAM_USERNAME_URL.match(account_url.strip())
        if m is None:
            raise HikerClientError(
                "not_found",
                f"account_url {account_url!r} is not a valid Instagram profile URL",
            )
        username = m.group("username")
        if username in {"p", "reel", "reels", "explore", "stories", "accounts"}:
            raise HikerClientError(
                "not_found",
                f"account_url {account_url!r} points to a post/reel, not a profile",
            )
        return username

    @staticmethod
    def _extract_user_id(payload: Any, username: str) -> str:
        """Pull `user_id` (as string) out of the `/v2/user/by/username` response.

        Payload shape verified at spike time; if the field lives at a
        different path (e.g. `user.pk` vs `pk`), one line change here.
        """
        if not isinstance(payload, dict):
            raise HikerClientError(
                "not_found",
                f"hikerapi user-by-username payload for {username!r} was not an object",
            )
        for candidate_key in ("id", "pk", "user_id"):
            value = payload.get(candidate_key)
            if value is not None:
                return str(value)
        # Some HikerAPI response envelopes wrap the user in a `user` sub-object.
        user_obj = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user_obj, dict):
            for candidate_key in ("id", "pk", "user_id"):
                value = user_obj.get(candidate_key)
                if value is not None:
                    return str(value)
        raise HikerClientError(
            "not_found",
            f"hikerapi user-by-username payload for {username!r} had no id field. "
            f"top-level keys: {sorted(payload.keys())}",
        )

    @staticmethod
    def _extract_items(page: Any) -> list[dict[str, Any]]:
        """Pull `items[]` out of the `/g2/user/medias` PageResponse envelope.

        Expected shape: `{"response": {"items": [...], ...}, "next_page_id": ...}`.
        If the envelope shape differs, one line change here.
        """
        if not isinstance(page, dict):
            raise HikerClientError("not_found", "hikerapi user-medias page was not an object")
        response_obj = page.get("response")
        items = response_obj.get("items") if isinstance(response_obj, dict) else page.get("items")
        if not isinstance(items, list):
            keys = sorted(page.keys())
            raise HikerClientError(
                "not_found",
                f"hikerapi user-medias page had no items[] list. top-level keys: {keys}",
            )
        return [item for item in items if isinstance(item, dict)]


def _default_now() -> datetime:
    return datetime.now(UTC)


def _shortcode_to_url(code: str) -> str:
    """Build a canonical Instagram post URL from a shortcode.

    HikerAPI's `code` field is the same shortcode `instaloader.Post.shortcode`
    exposes; the URL shape below is what M3's `extract_once` accepts.
    """
    return f"https://www.instagram.com/p/{code}/"
