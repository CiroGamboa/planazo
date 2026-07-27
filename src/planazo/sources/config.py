"""Pydantic-validated loader for `data/sources.yaml`.

`SourcesConfig` is the root model — a `sources` map whose keys are adapter
names (`"instagram"`, future `"tiktok"`, ...) and whose values are per-source
`SourceConfig` blocks. Each `SourceConfig` names the source-wide defaults
(`default_cadence`, `default_media_types`) plus a list of `AccountConfig`
entries, each of which may override the source-wide defaults for one target
URL. Malformed YAML raises `ValidationError` at `load_config()` time — before
any fetch runs, per AGENTS.md rule 1 (validate at the boundary) and rule 4
(typed error state, never a partial-config surprise).

Cadence strings accept a shorthand `<int>[smhd]` (`"6h"`, `"30m"`, `"7d"`)
alongside Pydantic's native ISO-8601 duration parsing (`"PT6H"`). The
shorthand parser runs as a `before` field validator — it recognizes the
`<int>[smhd]` shape and returns a `timedelta`; anything else falls through
to Pydantic's built-in timedelta parser, which still rejects nonsense.

`AccountConfig.resolved_cadence(source_defaults)` and
`.resolved_media_types(source_defaults)` fold per-account overrides into a
concrete value the scheduler can consume without knowing about the two-tier
default shape.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_CADENCE_SHORTHAND = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_CADENCE_UNITS: dict[str, str] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


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


class AccountConfig(BaseModel):
    """One target URL for a source, with optional per-account overrides."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    cadence: timedelta | None = None
    media_types: MediaTypeFlags | None = None

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


class SourceConfig(BaseModel):
    """One source (`instagram`, future `tiktok`, ...) — defaults + accounts."""

    model_config = ConfigDict(extra="forbid")

    default_cadence: timedelta
    default_media_types: MediaTypeFlags
    accounts: list[AccountConfig] = Field(default_factory=list)

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
