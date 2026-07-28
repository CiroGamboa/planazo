"""Structural contract for Instagram account-URL → post-URL discovery.

The scheduler (#67) owns discovery routing, not the adapter. `AccountConfig.backend`
picks between concrete implementations of `InstagramDiscoveryProtocol` at tick
time — the `hikerapi` backend (`HikerClient`, paid multi-key pool) or the
`anonymous` backend (`AnonInstagramClient`, `curl_cffi` + Meta's
`web_profile_info` endpoint). Both clients return `list[str]` of canonical
post URLs — never a `RawPost`, never a caption, never any text the LLM could
interpret as instructions (Rule 2 trust boundary).
"""

from __future__ import annotations

from typing import Protocol


class InstagramDiscoveryProtocol(Protocol):
    """Structural contract for account-URL → post-URL discovery.

    Any concrete discovery backend (HikerAPI, anonymous `curl_cffi`, or
    a future third implementation slotted in as a peer) conforms to this
    Protocol by exposing `list_recent_posts(account_url, limit)` with the
    same shape. The scheduler (#67) depends on this Protocol, not any
    concrete class.
    """

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]: ...
