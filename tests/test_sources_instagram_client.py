"""Unit tests for `InstagramClient` and the `InstaloaderPostView` boundary.

The client is the one place instaloader's exception surface is caught and
mapped to typed error branches. These tests do not touch the network — the
underlying `instaloader.Instaloader` is exercised only for session-cookie
planting; the exception-mapping tests parametrize over injected exception
classes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import instaloader
import pytest
from instaloader.exceptions import (
    LoginRequiredException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)
from pydantic import ValidationError

from planazo.sources.base import ErrorType
from planazo.sources.instagram.client import (
    InstagramClient,
    InstagramClientError,
)
from planazo.sources.instagram.model_view import (
    InstaloaderPostView,
    InstaloaderSidecarNodeView,
)

_STATIC_PAYLOAD = {
    "shortcode": "ABC123",
    "typename": "GraphImage",
    "caption": "hello world",
    "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
    "owner_username": "test_venue",
    "url": "https://scontent.cdninstagram.com/i.jpg",
    "video_url": None,
    "video_duration": None,
    "mediacount": 1,
    "sidecar_nodes": [],
}


def test_instaloader_post_view_accepts_static_post_payload() -> None:
    view = InstaloaderPostView.model_validate(_STATIC_PAYLOAD)

    assert view.shortcode == "ABC123"
    assert view.typename == "GraphImage"
    assert view.caption == "hello world"


def test_instaloader_post_view_rejects_missing_shortcode() -> None:
    payload = dict(_STATIC_PAYLOAD)
    payload.pop("shortcode")

    with pytest.raises(ValidationError):
        InstaloaderPostView.model_validate(payload)


def test_instaloader_post_view_rejects_missing_typename() -> None:
    payload = dict(_STATIC_PAYLOAD)
    payload.pop("typename")

    with pytest.raises(ValidationError):
        InstaloaderPostView.model_validate(payload)


def test_instaloader_post_view_accepts_unknown_typename() -> None:
    """`typename` validates as an open string — unknown values pass the boundary.

    Value-space routing lives in the adapter (`_route` returns
    `unsupported_media` for anything outside the three handled shapes); the
    Pydantic view only checks structure. A schema drift on Meta's side (a
    new post kind) surfaces as a typed adapter error at fetch time, not as
    a `ValidationError` in the client.
    """
    payload = dict(_STATIC_PAYLOAD)
    payload["typename"] = "GraphSomethingNew"

    view = InstaloaderPostView.model_validate(payload)

    assert view.typename == "GraphSomethingNew"


def test_instaloader_sidecar_node_view_accepts_image_node() -> None:
    node = InstaloaderSidecarNodeView.model_validate(
        {"is_video": False, "display_url": "https://example.com/x.jpg"}
    )

    assert node.is_video is False
    assert node.display_url == "https://example.com/x.jpg"


def test_client_load_session_from_env_no_env_var_leaves_client_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INSTAGRAM_SESSION_ID", raising=False)
    client = InstagramClient()

    client.load_session_from_env()

    assert client.session_loaded is False


def test_client_load_session_from_env_plants_cookie_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INSTAGRAM_SESSION_ID", "test-session-id")
    loader = instaloader.Instaloader()
    client = InstagramClient(loader=loader)

    client.load_session_from_env()

    assert client.session_loaded is True
    cookie_value = loader.context._session.cookies.get("sessionid", domain=".instagram.com")
    assert cookie_value == "test-session-id"


def test_instagram_client_error_carries_error_type() -> None:
    err = InstagramClientError("not_found", "post XYZ not found")

    assert err.error_type == "not_found"
    assert "post XYZ not found" in str(err)


@pytest.mark.parametrize(
    ("exception_class", "expected_error_type"),
    [
        (QueryReturnedNotFoundException, "not_found"),
        (TooManyRequestsException, "rate_limited"),
        (LoginRequiredException, "auth_failed"),
    ],
)
def test_client_maps_instaloader_exception_to_wrapper_error(
    monkeypatch: pytest.MonkeyPatch,
    exception_class: type[BaseException],
    expected_error_type: ErrorType,
) -> None:
    """`fetch_metadata` wraps the pinned-instaloader exception in a typed one.

    This is the one place the class-name reconciliation lives — the
    parametrization ranges over the exact classes we catch in `client.py`,
    so a rename on a future instaloader bump surfaces here first.
    """

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise exception_class("simulated failure from instaloader")

    monkeypatch.setattr(instaloader.Post, "from_shortcode", staticmethod(_raise))
    client = InstagramClient()

    with pytest.raises(InstagramClientError) as excinfo:
        client.fetch_metadata("ABC123")

    assert excinfo.value.error_type == expected_error_type
