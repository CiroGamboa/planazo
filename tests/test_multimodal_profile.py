"""Contract tests for `planazo.extraction.multimodal_profile`.

Locks:

- Field bounds — `>= 1` and `<= 30` on both fields; runaway-cost backstop.
- `SINGLE_POST` = 3/3 — the pre-profile default; carousel byte-identity in
  extractor tests depends on this exact value.
- `ACCOUNT_SCAN` — `max_carousel_images > SINGLE_POST.max_carousel_images`
  (the whole point) and `max_reel_frames > SINGLE_POST.max_reel_frames`.
- `resolve_profile` layers per-field overrides on top of a base preset;
  `None` inherits, a value overrides. Out-of-bounds overrides raise
  `ValidationError` at resolve time — never silently later at extraction.
- `AccountConfig.resolved_multimodal_profile` folds YAML overrides into a
  concrete profile without the caller knowing about the two-tier shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from planazo.extraction.multimodal_profile import (
    ACCOUNT_SCAN,
    SINGLE_POST,
    MultimodalProfile,
    resolve_profile,
)
from planazo.sources.config import AccountConfig

# ---- MultimodalProfile bounds --------------------------------------------


def test_zero_max_carousel_images_rejected() -> None:
    with pytest.raises(ValidationError):
        MultimodalProfile(max_carousel_images=0, max_reel_frames=3)


def test_zero_max_reel_frames_rejected() -> None:
    with pytest.raises(ValidationError):
        MultimodalProfile(max_carousel_images=3, max_reel_frames=0)


def test_negative_max_carousel_images_rejected() -> None:
    with pytest.raises(ValidationError):
        MultimodalProfile(max_carousel_images=-1, max_reel_frames=3)


def test_upper_bound_31_rejected() -> None:
    """The 30 cap is a runaway-cost backstop, not a policy target."""
    with pytest.raises(ValidationError):
        MultimodalProfile(max_carousel_images=31, max_reel_frames=3)
    with pytest.raises(ValidationError):
        MultimodalProfile(max_carousel_images=10, max_reel_frames=31)


def test_upper_bound_30_accepted() -> None:
    """The bound is inclusive on both ends — 30 lands, 31 does not."""
    profile = MultimodalProfile(max_carousel_images=30, max_reel_frames=30)
    assert profile.max_carousel_images == 30
    assert profile.max_reel_frames == 30


def test_frozen_prevents_mutation() -> None:
    """Presets are shared module-level constants — accidental mutation would
    silently change every downstream extractor's behavior."""
    with pytest.raises(ValidationError):
        SINGLE_POST.max_carousel_images = 99  # type: ignore[misc]


# ---- Named presets --------------------------------------------------------


def test_single_post_preset_values() -> None:
    """Locks `SINGLE_POST` at 3/3 — the pre-profile default. Byte-identical
    extractor tests depend on this exact value."""
    assert SINGLE_POST.max_carousel_images == 3
    assert SINGLE_POST.max_reel_frames == 3


def test_account_scan_preset_lifts_carousel_and_reel_caps() -> None:
    """`ACCOUNT_SCAN` must be strictly larger than `SINGLE_POST` on both
    fields — the whole reason the profile exists is to give roundup carousels
    enough slides."""
    assert ACCOUNT_SCAN.max_carousel_images > SINGLE_POST.max_carousel_images
    assert ACCOUNT_SCAN.max_reel_frames > SINGLE_POST.max_reel_frames


def test_account_scan_preset_locked_at_expected_values() -> None:
    """Guard the ACCOUNT_SCAN value change from PR #134 — the caps grew from
    10/6 to 20/10 to match typical curator carousels (25-30 slides). A
    future accidental revert to 10/6 would silently regress roundup
    coverage; this test breaks first."""
    assert ACCOUNT_SCAN.max_carousel_images == 20
    assert ACCOUNT_SCAN.max_reel_frames == 10


# ---- resolve_profile ------------------------------------------------------


def test_resolve_profile_inherits_when_both_overrides_none() -> None:
    """No overrides → identity: the resolved profile equals the base by value."""
    resolved = resolve_profile(SINGLE_POST)
    assert resolved == SINGLE_POST


def test_resolve_profile_applies_carousel_override_only() -> None:
    """Per-field independence: `max_carousel_images` overrides while
    `max_reel_frames` still inherits from the base."""
    resolved = resolve_profile(ACCOUNT_SCAN, max_carousel_images=15)
    assert resolved.max_carousel_images == 15
    assert resolved.max_reel_frames == ACCOUNT_SCAN.max_reel_frames


def test_resolve_profile_applies_reel_override_only() -> None:
    resolved = resolve_profile(ACCOUNT_SCAN, max_reel_frames=8)
    assert resolved.max_carousel_images == ACCOUNT_SCAN.max_carousel_images
    assert resolved.max_reel_frames == 8


def test_resolve_profile_applies_both_overrides() -> None:
    resolved = resolve_profile(SINGLE_POST, max_carousel_images=20, max_reel_frames=10)
    assert resolved.max_carousel_images == 20
    assert resolved.max_reel_frames == 10


def test_resolve_profile_out_of_bounds_override_raises_here() -> None:
    """A mis-configured YAML with `max_carousel_images: 100` raises at
    resolve time, not at extraction time. The runaway-cost backstop fires
    before the LLM is invoked."""
    with pytest.raises(ValidationError):
        resolve_profile(ACCOUNT_SCAN, max_carousel_images=100)


# ---- AccountConfig.resolved_multimodal_profile ---------------------------


def test_account_config_no_override_inherits_base() -> None:
    """An account with no YAML overrides folds to the base preset unchanged."""
    account = AccountConfig(url="https://instagram.com/somevenue")

    resolved = account.resolved_multimodal_profile(ACCOUNT_SCAN)

    assert resolved == ACCOUNT_SCAN


def test_account_config_carousel_override_layers_on_top_of_base() -> None:
    """Roundup-account shape: caller passes `ACCOUNT_SCAN` as base, the
    account's YAML `max_carousel_images: 15` overrides it, and the resolved
    profile carries the higher cap into the multimodal hook."""
    account = AccountConfig(
        url="https://instagram.com/planesenbarcelona",
        max_carousel_images=15,
    )

    resolved = account.resolved_multimodal_profile(ACCOUNT_SCAN)

    assert resolved.max_carousel_images == 15
    assert resolved.max_reel_frames == ACCOUNT_SCAN.max_reel_frames


def test_account_config_reel_override_layers_on_top_of_base() -> None:
    account = AccountConfig(
        url="https://instagram.com/reelheavyaccount",
        max_reel_frames=10,
    )

    resolved = account.resolved_multimodal_profile(ACCOUNT_SCAN)

    assert resolved.max_carousel_images == ACCOUNT_SCAN.max_carousel_images
    assert resolved.max_reel_frames == 10


def test_account_config_out_of_bounds_carousel_override_rejected_at_load() -> None:
    """Bounds validation runs at Pydantic validation time — a `sources.yaml`
    with `max_carousel_images: 100` fails to load, not at scheduler-tick
    time. Same posture as the profile module's own upper-bound test."""
    with pytest.raises(ValidationError):
        AccountConfig(
            url="https://instagram.com/somevenue",
            max_carousel_images=100,
        )


def test_account_config_out_of_bounds_reel_override_rejected_at_load() -> None:
    with pytest.raises(ValidationError):
        AccountConfig(
            url="https://instagram.com/somevenue",
            max_reel_frames=100,
        )
