"""HikerAPI-backed Instagram discovery client (spike wiring).

Purpose (M3.5): given an Instagram account URL, return the last N post URLs so
the scheduler (#67) can iterate them without depending on the burner
(`instagrapi`) or the anonymous path's business-account block. Extraction of
each returned post URL is still handled by the existing `InstagramClient` +
`instaloader.Post.from_shortcode` — this client's ONLY job is discovery.

Endpoint choices (per OpenAPI at api.hikerapi.com/openapi.json):

- `GET /v2/user/by/username?username={u}` — username → user_id lookup.
- `GET /g2/user/medias?user_id={id}` — user_id → PageResponse of media
  objects. Each item has a `.code` field (the Instagram shortcode). We build
  the canonical URL `https://www.instagram.com/p/{code}/` from the code.

Exception mapping (aligned with `client.py`'s `ErrorType` taxonomy from
`sources/base.py`):

| HTTP status / condition       | ErrorType             | Notes                                    |
|-------------------------------|-----------------------|------------------------------------------|
| 401 (invalid api key)         | `auth_failed`         | HikerAPI rejected the header             |
| 403 (subscription / quota)    | `auth_failed`         | Account cannot use this endpoint         |
| 404 (username not found)      | `not_found`           | Instagram username does not exist        |
| 422 (validation error body)   | `not_found`           | Malformed username / user_id             |
| 429 (rate limit)              | `rate_limited`        | HikerAPI rate limit                      |
| 5xx or network error          | `rate_limited`        | Transient; retry later                   |
| response empty / no `items[]` | `not_found`           | Meta returned no medias for this account |

Config discipline (mirrors ADR 0006 §Decision 5 for `INSTAGRAM_SESSION_ID`):
- `PLANAZO_IG_HIKER_API_KEY` env var. Absent → `RuntimeError` at construction;
  callers wire this at the composition root, not inside the tick loop.

Rate-limit + cost note: every discovery call is TWO HikerAPI requests
(username-lookup + medias-fetch). At $0.02-0.03/req that's $0.04-0.06 per
account per tick. With 3 blocked-venue accounts on 6h cadence: ~$1-2/month.
`.env` documents the pricing model; the scheduler owns the cadence gating.

Spike status: unvalidated. Header name (`x-access-key`) assumed from OpenAPI
`APIKeyHeader` scheme naming convention — will confirm on the first live call
by observing which header the API accepts. If HikerAPI uses a different name
(e.g. `Authorization` bearer, `x-api-key`), the fix is one constant swap.
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar, Final, Protocol

import httpx

from planazo.sources.base import ErrorType

_INSTAGRAM_USERNAME_URL: Final = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?P<username>[A-Za-z0-9._]+)/?(?:\?.*)?$"
)


class HikerClientError(Exception):
    """Wrapper exception carrying an `ErrorType` — matches `InstagramClientError`."""

    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type: ErrorType = error_type


class InstagramDiscoveryProtocol(Protocol):
    """Structural contract for account-URL → post-URL discovery.

    Any concrete discovery backend (HikerAPI, anonymous `curl_cffi`, future
    `instagrapi` when the burner is warm) conforms to this Protocol by
    exposing `list_recent_posts(account_url, limit)` with the same shape.
    The scheduler (#67) depends on this Protocol, not any concrete class.
    """

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]: ...


class HikerClient:
    """HikerAPI discovery client — one place for the HikerAPI HTTP contract.

    Constructor accepts an optional `http_client` for tests to inject a fake
    (any object exposing `httpx.Client`-shaped `.get(path, params=)` — swap
    seam preserved).

    Read `PLANAZO_IG_HIKER_API_KEY` from the environment at construction time.
    Absent → `RuntimeError`; the scheduler's composition root is responsible
    for ensuring the key is present when this backend is configured.
    """

    BASE_URL: ClassVar[str] = "https://api.hikerapi.com"
    _API_KEY_HEADER: ClassVar[str] = "x-access-key"
    _TIMEOUT_SECONDS: ClassVar[float] = 15.0

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("PLANAZO_IG_HIKER_API_KEY")
        if not key:
            raise RuntimeError(
                "PLANAZO_IG_HIKER_API_KEY not set — cannot construct HikerClient. "
                "Add it to .env or pass api_key= explicitly."
            )
        self._api_key = key
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.Client(
                base_url=self.BASE_URL,
                timeout=self._TIMEOUT_SECONDS,
                headers={self._API_KEY_HEADER: self._api_key},
            )

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        """Return the last `limit` post URLs for the account at `account_url`.

        Two HikerAPI calls: username→user_id, then user_id→medias page.
        Result URLs are canonical `https://www.instagram.com/p/{code}/` (no
        `?hl=` query, no `img_index=` fragment) — the shape `extract_once`
        already handles.

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

    def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        """HTTP GET with the shared error-mapping table.

        Kept private — every response the caller sees has already been mapped
        through the taxonomy. The public methods above never see raw HTTP.
        """
        try:
            response = self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise HikerClientError("rate_limited", f"hikerapi timeout on {path} ({exc})") from exc
        except httpx.RequestError as exc:
            raise HikerClientError(
                "rate_limited", f"hikerapi network error on {path} ({exc})"
            ) from exc

        status = response.status_code
        if status == 401:
            raise HikerClientError(
                "auth_failed",
                f"hikerapi rejected api key on {path} (401) — check PLANAZO_IG_HIKER_API_KEY",
            )
        if status == 403:
            raise HikerClientError(
                "auth_failed",
                f"hikerapi forbade access on {path} (403) — subscription / quota",
            )
        if status == 404:
            raise HikerClientError("not_found", f"hikerapi 404 on {path} params={params}")
        if status == 422:
            raise HikerClientError(
                "not_found",
                f"hikerapi validation error on {path} ({response.text[:200]!r})",
            )
        if status == 429:
            raise HikerClientError("rate_limited", f"hikerapi rate-limited on {path} (429)")
        if 500 <= status < 600:
            raise HikerClientError(
                "rate_limited",
                f"hikerapi server error on {path} ({status})",
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
                "not_found",
                f"hikerapi returned non-JSON body on {path} ({exc})",
            ) from exc

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


def _shortcode_to_url(code: str) -> str:
    """Build a canonical Instagram post URL from a shortcode.

    HikerAPI's `code` field is the same shortcode `instaloader.Post.shortcode`
    exposes; the URL shape below is what M3's `extract_once` accepts.
    """
    return f"https://www.instagram.com/p/{code}/"
