"""Anonymous Instagram discovery client via `curl_cffi` + `web_profile_info`.

Purpose (M3.5): given an Instagram account URL, return the last N post URLs
without a paid HikerAPI key. Hits Meta's public
`https://www.instagram.com/api/v1/users/web_profile_info/`
endpoint (the same one the mobile-web UI uses) with a `curl_cffi.Session`
impersonating Chrome — Meta's soft-ban heuristics key on TLS fingerprints
and header ordering, both of which `curl_cffi`'s Chrome-impersonate profile
mimics.

Business venue accounts (`sala_apolo`, `razzmatazzclubs`, etc.) return HTTP
400 with a body naming `laser.provider` — Meta's schema block for
`ig_business_account`-tagged profiles that the anonymous endpoint refuses to
serve. Those accounts must route through the paid HikerAPI backend instead
(`AccountConfig.backend: "hikerapi"`); this client surfaces the block as a
typed `unsupported_media` error so the scheduler can log the routing miss.

Exception mapping (aligned with `HikerClient`'s `ErrorType` taxonomy from
`sources/base.py`):

| HTTP status / condition                        | ErrorType             |
|------------------------------------------------|-----------------------|
| 400 with `laser.provider` in body              | `unsupported_media`   |
| 400 with `ig_business_category_subvertical`   | `unsupported_media`   |
| 401 (Meta soft-ban / auth challenge)           | `auth_failed`         |
| 404 (username not found)                       | `not_found`           |
| 429 (rate limit)                               | `rate_limited`        |
| 5xx or network error                           | `rate_limited`        |
| Missing `edge_owner_to_timeline_media` path    | `not_found`           |

The client is I/O-only — it returns `list[str]` (canonical post URLs), never
a `RawPost`, never a caption, never anything the LLM could interpret as
instructions (Rule 2 trust boundary).

`x-ig-app-id` is the constant Meta's mobile-web UI ships today (`936619743392459`).
Meta rotates it every 2-4 weeks historically; the `not_found` mapping on missing
schema is the operator's signal to bump the constant + tests.
"""

from __future__ import annotations

import re
from typing import Any, Final, Protocol

from planazo.sources.base import ErrorType
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol

__all__ = [
    "AnonInstagramClient",
    "AnonInstagramClientError",
    "InstagramDiscoveryProtocol",
]

WEB_PROFILE_INFO_URL_TEMPLATE: Final[str] = (
    "https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
)
IG_APP_ID: Final[str] = "936619743392459"
IMPERSONATE_PROFILE: Final = "chrome"
TIMEOUT_SECONDS: Final[float] = 15.0

_INSTAGRAM_USERNAME_URL: Final = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?P<username>[A-Za-z0-9._]+)/?(?:\?.*)?$"
)

_LASER_PROVIDER_MARKERS: Final[tuple[str, ...]] = (
    "laser.provider",
    "ig_business_category_subvertical",
)


class _HttpResponse(Protocol):
    """Structural contract for the response `http_get` returns.

    Both `curl_cffi.requests.Response` and the test doubles satisfy this
    Protocol by exposing `status_code`, `text`, and `json()`.
    """

    status_code: int
    text: str

    def json(self) -> Any: ...


class _HttpGet(Protocol):
    def __call__(self, url: str, headers: dict[str, str]) -> _HttpResponse: ...


class AnonInstagramClientError(Exception):
    """Wrapper exception carrying an `ErrorType` — mirrors `HikerClientError`."""

    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type: ErrorType = error_type


class AnonInstagramClient:
    """Anonymous Instagram discovery client.

    Uses `curl_cffi.requests.Session(impersonate="chrome")` under the hood to
    defeat Meta's soft-ban TLS fingerprinting. Tests inject a fake `http_get`
    that returns a stub object with `.status_code`, `.text`, and `.json()`.
    """

    def __init__(self, *, http_get: _HttpGet | None = None) -> None:
        self._http_get: _HttpGet = http_get if http_get is not None else _default_http_get()

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        """Return the last `limit` post URLs for the account at `account_url`.

        One HTTP call to `web_profile_info`. Result URLs are canonical
        `https://www.instagram.com/p/{shortcode}/` — the shape `extract_once`
        already handles.

        Raises `AnonInstagramClientError` with the mapped `ErrorType` on any
        documented failure branch.
        """
        username = self._extract_username(account_url)
        url = WEB_PROFILE_INFO_URL_TEMPLATE.format(username=username)
        headers = {"x-ig-app-id": IG_APP_ID}
        try:
            response = self._http_get(url, headers)
        except Exception as exc:
            # curl_cffi raises curl_cffi.CurlError for network-level failures;
            # keep the mapping catch-all so a swapped-in transport (or a Meta-
            # side connection reset) surfaces as a typed rate_limited branch
            # rather than a raw exception on the tick loop.
            raise AnonInstagramClientError(
                "rate_limited",
                f"web_profile_info network error for {username!r} ({exc})",
            ) from exc

        status = response.status_code
        if status == 400:
            body_text = response.text or ""
            if any(marker in body_text for marker in _LASER_PROVIDER_MARKERS):
                raise AnonInstagramClientError(
                    "unsupported_media",
                    f"web_profile_info refused business-account {username!r} "
                    f"(laser.provider block; route via hikerapi backend)",
                )
            raise AnonInstagramClientError(
                "rate_limited",
                f"web_profile_info returned 400 for {username!r} ({body_text[:200]!r})",
            )
        if status == 401:
            raise AnonInstagramClientError(
                "auth_failed",
                f"web_profile_info returned 401 for {username!r} — Meta soft-ban signal",
            )
        if status == 404:
            raise AnonInstagramClientError(
                "not_found",
                f"web_profile_info returned 404 for {username!r} — username does not exist",
            )
        if status == 429:
            raise AnonInstagramClientError(
                "rate_limited",
                f"web_profile_info rate-limited for {username!r} (429)",
            )
        if 500 <= status < 600:
            raise AnonInstagramClientError(
                "rate_limited",
                f"web_profile_info server error for {username!r} ({status})",
            )
        if status != 200:
            raise AnonInstagramClientError(
                "rate_limited",
                f"web_profile_info unexpected status {status} for {username!r}",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnonInstagramClientError(
                "not_found",
                f"web_profile_info returned non-JSON body for {username!r} ({exc})",
            ) from exc

        shortcodes = self._extract_shortcodes(payload, username)
        return [_shortcode_to_url(code) for code in shortcodes[:limit]]

    @staticmethod
    def _extract_username(account_url: str) -> str:
        """Parse the username from an Instagram account URL.

        Accepts trailing slash and `?hl=xx-yy` variants; rejects post URLs
        (`/p/{shortcode}/`) and reel URLs (`/reel/{shortcode}/`) which are
        not the shape this method takes.
        """
        m = _INSTAGRAM_USERNAME_URL.match(account_url.strip())
        if m is None:
            raise AnonInstagramClientError(
                "not_found",
                f"account_url {account_url!r} is not a valid Instagram profile URL",
            )
        username = m.group("username")
        if username in {"p", "reel", "reels", "explore", "stories", "accounts"}:
            raise AnonInstagramClientError(
                "not_found",
                f"account_url {account_url!r} points to a post/reel, not a profile",
            )
        return username

    @staticmethod
    def _extract_shortcodes(payload: Any, username: str) -> list[str]:
        """Pull `data.user.edge_owner_to_timeline_media.edges[*].node.shortcode`.

        `.shortcode` is the primary field; some rotations expose `.code`
        instead — the parser accepts either so a Meta rotation surfaces later
        than it otherwise would. A top-level shape change (missing edges path)
        maps to `not_found` — the schema-drift signal.
        """
        if not isinstance(payload, dict):
            raise AnonInstagramClientError(
                "not_found",
                f"web_profile_info payload for {username!r} was not an object",
            )
        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        media = user.get("edge_owner_to_timeline_media") if isinstance(user, dict) else None
        edges = media.get("edges") if isinstance(media, dict) else None
        if not isinstance(edges, list):
            raise AnonInstagramClientError(
                "not_found",
                f"web_profile_info payload for {username!r} had no "
                f"data.user.edge_owner_to_timeline_media.edges list",
            )
        shortcodes: list[str] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict):
                continue
            code = node.get("shortcode") or node.get("code")
            if isinstance(code, str) and code:
                shortcodes.append(code)
        return shortcodes


def _default_http_get() -> _HttpGet:
    """Build the production `http_get` closure over a Chrome-impersonate session.

    Imported lazily so tests that inject a fake don't pay the `curl_cffi`
    import cost, and so a missing `curl_cffi` dependency surfaces at construction
    time rather than at import time.
    """
    # curl_cffi's session/response are dynamically typed (no useful stubs
    # for our purposes); `session` is treated as `Any` at this boundary so the
    # closure below returns a duck-typed `_HttpResponse` shim.
    from curl_cffi import requests as curl_requests

    session: Any = curl_requests.Session(impersonate=IMPERSONATE_PROFILE)

    def _get(url: str, headers: dict[str, str]) -> _HttpResponse:
        response: _HttpResponse = session.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        return response

    return _get


def _shortcode_to_url(code: str) -> str:
    """Build a canonical Instagram post URL from a shortcode."""
    return f"https://www.instagram.com/p/{code}/"
