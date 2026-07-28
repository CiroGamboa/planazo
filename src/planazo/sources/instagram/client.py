"""Thin wrapper around `instaloader.Instaloader` — the scraper swap point.

Every `instaloader` call the adapter needs goes through `InstagramClient`; if a
future ADR replaces the scraper with Playwright or `instagrapi`, only this
module changes. The wrapper does three things:

1. Loads a session from `INSTAGRAM_SESSION_ID` when the env var is set; falls
   back to anonymous mode when absent (ADR 0006, decision 5). Anonymous fetch
   works for many public static posts but rate-limits faster and returns
   `auth_failed` for post types that require a logged-in session.
2. Fetches one post's metadata by shortcode, projects the fields we consume
   through `InstaloaderPostView`, and returns the view (AGENTS.md rule 1 —
   validate at the boundary). Callers never see a raw `instaloader.Post`.
3. Exposes `InstagramClientError` — one wrapper exception carrying an
   `ErrorType` — so the adapter can `except InstagramClientError` once instead
   of importing instaloader's exception surface. The exception-to-branch
   mapping table lives here and is the one file that changes when the pinned
   instaloader version's exception names shift (ADR 0006, decision 6).

Reconciled against `instaloader==4.15.3`:

| Exception (or condition)              | ErrorType             |
|---------------------------------------|-----------------------|
| URL host not `instagram.com`          | `unsupported_source`  |
| `QueryReturnedNotFoundException`      | `not_found`           |
| `TooManyRequestsException`            | `rate_limited`        |
| `LoginRequiredException`              | `auth_failed`         |
| `typename` outside the routed set     | `unsupported_media`   |

The two condition branches (URL host, `typename`) are decided in the adapter,
not here.

`QueryReturnedNotFoundException` and `TooManyRequestsException` both subclass
`instaloader.exceptions.ConnectionException`; catching them by their specific
types is intentional so a generic connection failure surfaces (uncaught) as an
unhandled exception rather than being silently mislabelled `rate_limited`.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import instaloader
from instaloader.exceptions import (
    LoginRequiredException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)

from planazo.sources.base import ErrorType
from planazo.sources.instagram.model_view import InstaloaderPostView


class InstagramClientError(Exception):
    """One wrapper exception carrying the reconciled `ErrorType` branch.

    The adapter's `fetch_post` catches this once and maps `error_type` into a
    typed error dict. Instaloader's own exception surface never leaks past
    this module.
    """

    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type: ErrorType = error_type


class InstagramClientProtocol(Protocol):
    """Structural contract for the client the adapter consumes.

    The concrete `InstagramClient` below conforms; test fakes conform by
    exposing `fetch_metadata` with the same shape. Callers depend on this
    Protocol, not the concrete class — that keeps the swap point one type
    substitution away.
    """

    def fetch_metadata(self, shortcode: str) -> InstaloaderPostView: ...


class InstagramClient:
    """Isolated `instaloader` call surface — the one place scraper choice lives.

    Constructor accepts an optional `loader` for tests to inject a fake with
    the same shape; production callers instantiate it with defaults.
    """

    def __init__(self, loader: instaloader.Instaloader | None = None) -> None:
        self._loader = loader if loader is not None else instaloader.Instaloader()
        self._session_loaded = False

    def load_session_from_env(self) -> None:
        """Load a session cookie from `INSTAGRAM_SESSION_ID` when set.

        Absent env var is not an error — the client stays anonymous. When the
        value is present, a `sessionid` cookie is planted on the underlying
        `requests.Session`; instaloader picks it up on the next request. No
        `.session` files are touched (ADR 0006, decision 5).
        """
        session_id = os.environ.get("INSTAGRAM_SESSION_ID")
        if not session_id:
            return
        # `context._session` is instaloader's underlying `requests.Session`. It
        # is a leading-underscore attribute — instaloader offers no public
        # accessor. If a future instaloader release renames or restructures it,
        # this planter fails and `session_loaded` stays False; the scraper falls
        # back to anonymous mode. Add a fallback here (or supersede ADR 0006)
        # when it happens.
        session = self._loader.context._session
        session.cookies.set("sessionid", session_id, domain=".instagram.com")
        self._session_loaded = True

    @property
    def session_loaded(self) -> bool:
        """Whether a session cookie was planted from the env var."""
        return self._session_loaded

    def fetch_metadata(self, shortcode: str) -> InstaloaderPostView:
        """Fetch one post's metadata by shortcode, validated through `InstaloaderPostView`.

        Raises `InstagramClientError` on any of the four instaloader
        exceptions in the mapping table. Everything else — genuine network
        failures, schema drift caught by `InstaloaderPostView`, unmapped
        instaloader exceptions — surfaces uncaught so an unknown failure
        mode is visible rather than silently coerced.
        """
        try:
            post = instaloader.Post.from_shortcode(self._loader.context, shortcode)
            return InstaloaderPostView.model_validate(_project_post(post))
        except QueryReturnedNotFoundException as exc:
            raise InstagramClientError(
                "not_found", f"instagram post {shortcode!r} not found"
            ) from exc
        except TooManyRequestsException as exc:
            raise InstagramClientError(
                "rate_limited", f"instagram rate-limited on {shortcode!r}"
            ) from exc
        except LoginRequiredException as exc:
            raise InstagramClientError(
                "auth_failed", f"instagram login required for {shortcode!r}"
            ) from exc


def _project_post(post: instaloader.Post) -> dict[str, Any]:
    """Copy the fields we consume off an `instaloader.Post` into a dict.

    `sidecar_nodes` is materialised as a list here rather than left as a
    generator so `InstaloaderPostView` can revalidate the payload without
    re-iterating instaloader's cursor. Non-sidecar posts leave the field
    empty.
    """
    payload: dict[str, Any] = {
        "shortcode": post.shortcode,
        "typename": post.typename,
        "caption": post.caption,
        "date_utc": post.date_utc,
        "owner_username": post.owner_username,
        "url": post.url,
        "video_url": post.video_url,
        "video_duration": post.video_duration,
        "mediacount": post.mediacount,
    }
    if post.typename == "GraphSidecar":
        payload["sidecar_nodes"] = [
            {
                "is_video": node.is_video,
                "display_url": node.display_url,
                "video_url": node.video_url,
                "video_duration": None,
            }
            for node in post.get_sidecar_nodes()
        ]
    else:
        payload["sidecar_nodes"] = []
    return payload
