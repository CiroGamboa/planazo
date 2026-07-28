"""Pydantic-validated loader for `data/sources.yaml`.

`SourcesConfig` is the root model — a `sources` map whose keys are adapter
names (`"instagram"`, future `"tiktok"`, ...) and whose values are per-source
`SourceConfig` blocks. Each `SourceConfig` names the source-wide defaults
(`default_cadence`, `default_media_types`) plus two independently-populated
work-lists the scheduler consumes on every tick:

- `accounts: list[AccountConfig]` — accounts to scan for recent posts.
  Discovery lives in the `scheduler/` bounded context: each account routes
  through the configured `AccountConfig.backend` to one of the two
  `InstagramDiscoveryProtocol` implementations the scheduler composes
  (`anonymous` via `curl_cffi` + Meta's `web_profile_info`; `hikerapi` via
  the paid multi-key pool). See ADR 0011 + ADR 0014.
- `posts: list[PostConfig]` — explicit post URLs to (re-)extract. Works
  from anonymous access (`Post.from_shortcode`) and skips discovery
  entirely. Successful extractions are locked out by the composite
  `UNIQUE(source_url, event_index_in_post)` in `events`; the entry's
  `cadence` only gates failure-retry.

Both lists default to empty — a config with only `accounts:`, only
`posts:`, both, or neither is valid.

Malformed YAML raises `ValidationError` at `load_config()` time — before
any fetch runs, per AGENTS.md rule 1 (validate at the boundary) and rule 4
(typed error state, never a partial-config surprise). `PostConfig.url` is
validated against the `instagram.com/{p|reel}/{shortcode}/` shape at load
time so a typo (an account URL pasted into `posts:`) surfaces as a
`ValidationError`, not a runtime `not_found`. `is_instagram_post_url(url)`
exposes the same shape check for callers that need to discriminate a
loose URL (e.g. the `planazo-scheduler --once <url>` CLI branch).

Cadence strings accept a shorthand `<int>[smhd]` (`"6h"`, `"30m"`, `"7d"`)
alongside Pydantic's native ISO-8601 duration parsing (`"PT6H"`). The
shorthand parser runs as a `before` field validator — it recognizes the
`<int>[smhd]` shape and returns a `timedelta`; anything else falls through
to Pydantic's built-in timedelta parser, which still rejects nonsense.

`AccountConfig.resolved_cadence(source_defaults)` and
`.resolved_media_types(source_defaults)` fold per-account overrides into a
concrete value the scheduler can consume without knowing about the two-tier
default shape. `PostConfig.resolved_cadence(source_defaults)` folds the
same way for post entries.

`enumerate_configured_posts(source_config)` returns the flat list of post
URLs in config order — the scheduler consumes it alongside every
`AccountConfig` entry's discovery output to build the per-tick work-list.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_CADENCE_SHORTHAND = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_CADENCE_UNITS: dict[str, str] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}

# Instagram post URLs the operator can paste into `posts:`. `p` covers static
# posts and carousels; `reel` covers reels. `tv` is deliberately excluded here
# because operators paste `/p/` or `/reel/` URLs from the app — the adapter
# still accepts `/tv/` at fetch time for historical URLs (see
# `sources.instagram.adapter._INSTAGRAM_URL`), but the config-layer validator
# is intentionally narrower so a mistyped account URL cannot slip through.
_INSTAGRAM_POST_URL = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel)/[A-Za-z0-9_-]+(?:[/?#]|$)"
)


def _parse_cadence(value: Any) -> Any:
    """Accept `<int>[smhd]` shorthand and forward everything else unchanged.

    Pydantic's built-in `timedelta` parser handles ISO-8601 durations and
    already-typed `timedelta` values; the shorthand parser only intercepts
    string values matching the shorthand pattern so a malformed string like
    `"6 hours"` still surfaces as a `ValidationError` from Pydantic itself.
    """
    if isinstance(value, str):
        match = _CADENCE_SHORTHAND.match(value)
        if match:
            amount = int(match.group(1))
            unit = _CADENCE_UNITS[match.group(2)]
            return timedelta(**{unit: amount})
    return value


class MediaTypeFlags(BaseModel):
    """Which of the four Instagram post types an adapter attempts.

    All four default to `True`; a per-account override typically flips one
    to `False` when a venue never posts that kind, saving the adapter a
    round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    static_posts: bool = True
    reels: bool = True
    carousels: bool = True
    video_posts: bool = True

    def enabled_kinds(self) -> list[str]:
        """The enabled media-kind names in declaration order.

        Single source of truth for both the adapter's `plan_for` and the
        CLI's `--dry-run` — callers that need the enabled subset iterate
        this list rather than reflecting on the model.
        """
        fields = ("static_posts", "reels", "carousels", "video_posts")
        return [name for name in fields if getattr(self, name)]


class AccountConfig(BaseModel):
    """One target URL for a source, with optional per-account overrides.

    `backend` picks the discovery backend the scheduler routes to for this
    account: `"anonymous"` (default; `curl_cffi` + Meta's `web_profile_info`)
    or `"hikerapi"` (paid multi-key pool). Business venue accounts must
    route via `hikerapi` — the anonymous endpoint refuses them with a
    `laser.provider` schema block.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    cadence: timedelta | None = None
    media_types: MediaTypeFlags | None = None
    backend: Literal["anonymous", "hikerapi"] = "anonymous"

    @field_validator("cadence", mode="before")
    @classmethod
    def _shorthand_cadence(cls, value: Any) -> Any:
        return _parse_cadence(value)

    def resolved_cadence(self, source_defaults: SourceConfig) -> timedelta:
        """The cadence the scheduler should use for this account.

        Returns the per-account override when set, the source-wide default
        otherwise.
        """
        if self.cadence is not None:
            return self.cadence
        return source_defaults.default_cadence

    def resolved_media_types(self, source_defaults: SourceConfig) -> MediaTypeFlags:
        """The media-type flags the adapter should attempt for this account."""
        if self.media_types is not None:
            return self.media_types
        return source_defaults.default_media_types


class PostConfig(BaseModel):
    """One explicit post URL for the scheduler's URL-list mode.

    The `posts:` block skips discovery entirely — the operator pastes
    specific post URLs and each tick sends them straight to `extract_once`
    without walking any account backend. Successful extractions are
    naturally idempotent via the composite
    `UNIQUE(source_url, event_index_in_post)` in `events`; the entry's
    `cadence` only gates how often a *failed* extraction (rate-limit,
    transient network error) is retried.

    `url` is validated against the `instagram.com/{p|reel}/{shortcode}/`
    shape so a mistyped account URL surfaces as a `ValidationError` at
    load time, not a runtime `not_found`.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    cadence: timedelta | None = None

    @field_validator("cadence", mode="before")
    @classmethod
    def _shorthand_cadence(cls, value: Any) -> Any:
        return _parse_cadence(value)

    @field_validator("url")
    @classmethod
    def _url_must_be_post_or_reel(cls, value: str) -> str:
        if _INSTAGRAM_POST_URL.match(value) is None:
            raise ValueError(
                f"PostConfig.url must be an Instagram post URL of the shape "
                f"'https://[www.]instagram.com/p/<shortcode>/' or "
                f"'https://[www.]instagram.com/reel/<shortcode>/'; "
                f"got {value!r} — account URLs belong under 'accounts:', "
                f"not 'posts:'"
            )
        return value

    def resolved_cadence(self, source_defaults: SourceConfig) -> timedelta:
        """The retry-on-failure cadence for this post.

        Returns the per-post override when set, the source-wide default
        otherwise. Only load-bearing for the failure-retry path — a
        successfully extracted post is locked out by the composite
        `UNIQUE(source_url, event_index_in_post)` in `events`.
        """
        if self.cadence is not None:
            return self.cadence
        return source_defaults.default_cadence


class SourceConfig(BaseModel):
    """One source (`instagram`, future `tiktok`, ...) — defaults + work-lists.

    Two independently-populated work-lists, either or both may be empty:

    - `accounts` — accounts to scan for recent posts (routes through the
      scheduler's discovery backends via `AccountConfig.backend`).
    - `posts` — explicit post URLs to (re-)extract (skips discovery; works
      from anonymous `Post.from_shortcode`).
    """

    model_config = ConfigDict(extra="forbid")

    default_cadence: timedelta
    default_media_types: MediaTypeFlags
    accounts: list[AccountConfig] = Field(default_factory=list)
    posts: list[PostConfig] = Field(default_factory=list)

    @field_validator("default_cadence", mode="before")
    @classmethod
    def _shorthand_default_cadence(cls, value: Any) -> Any:
        return _parse_cadence(value)


class SourcesConfig(BaseModel):
    """Root of `data/sources.yaml` — a name-keyed map of source blocks."""

    model_config = ConfigDict(extra="forbid")

    sources: dict[str, SourceConfig] = Field(default_factory=dict)


def load_config(path: Path = Path("data/sources.yaml")) -> SourcesConfig:
    """Read + validate the sources config; raise `ValidationError` on any issue.

    Fails at boot rather than lazily on the first fetch: a typo in cadence
    (`"6 hours"` instead of `"6h"`), an unknown media-type flag, or a missing
    required field all raise before any adapter is registered.
    """
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        raw = {}
    return SourcesConfig.model_validate(raw)


def enumerate_configured_posts(source_config: SourceConfig) -> list[str]:
    """Return the flat list of post URLs from `source_config.posts`, in order.

    Pure function — no I/O, no side effects. The scheduler (#67) consumes
    it alongside each `AccountConfig` entry's discovery-backend output to
    build the per-tick work-list.
    """
    return [entry.url for entry in source_config.posts]


def is_instagram_post_url(url: str) -> bool:
    """True when `url` matches the shape `PostConfig.url` accepts.

    Reuses the same compiled regex `PostConfig`'s field validator applies
    at load time, so a caller (the `planazo-scheduler --once <url>` CLI
    branch) uses one canonical discriminator between post URLs and
    account URLs instead of re-inventing the pattern. Returns `False` for
    account URLs, garbage strings, and `/tv/` legacy URLs — an operator
    calling `--once` with an account URL falls through to the account-URL
    branch (which looks it up in `sources.yaml`).
    """
    return _INSTAGRAM_POST_URL.match(url) is not None
