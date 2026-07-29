"""Configurable multimodal caps for the Extractor.

The Extractor's `_build_multimodal_hook` in `agents/extractor.py` sends a
bounded number of images to the LLM: `max_carousel_images` slides for a
carousel post, `max_reel_frames` frames for a reel. Those numbers used to
live as module-level `Final` constants (`MAX_CAROUSEL_IMAGES = 3`,
`MAX_REEL_FRAMES = 3`), which forced every entry point through the same
budget regardless of what shape of content it was extracting.

That default is right for a single-venue post — one flyer + a caption
that already tells the story. It is wrong for a curator / roundup account
that posts 20-30 slides where each slide is a distinct event
(`@planesenbarcelona` was the load-bearing example on the M6 demo run).
With the fixed budget the LLM saw 3 of 29 slides, correctly returned
`needs_clarification: ambiguous_content`, and zero events landed.

`MultimodalProfile` is the surface the composition roots use to pick the
right budget per entry point:

- `SINGLE_POST` (3 / 3) — byte-identical to the pre-profile defaults.
  Used by `--tick` post-only extraction, `--once <post-url>`, and the
  Recommender's `dispatch_extraction` tool.
- `ACCOUNT_SCAN` (5 / 5) — the default for `--scan-account` and the
  `--tick` account-discovery path. Demo-tuned for fast LLM turnaround;
  paired with the partial-save discipline in the delegation brief, this
  yields enumerated events per roundup on a light visual budget. For
  production coverage of 25-30 slide agendas, raise per-account via
  `AccountConfig.max_carousel_images` in `data/sources.yaml`.

Per-account overrides layer on top of a base preset via
`resolve_profile`. A `sources.yaml` account entry can set
`max_carousel_images` or `max_reel_frames` independently; a missing
field inherits the base preset's value.

Bounds are 1..=30 inclusive on both fields — the upper bound is a
runaway-cost backstop, not a policy target. A YAML entry with
`max_carousel_images: 100` fails Pydantic validation at load time
rather than at scheduler-tick time.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

_MIN_IMAGES: Final[int] = 1
_MAX_IMAGES: Final[int] = 30


class MultimodalProfile(BaseModel):
    """Bounded caps on how many images the Extractor sends to the LLM per post.

    Threaded through `extract_once(..., profile=...)` into
    `_build_multimodal_hook`. Every field is `>= 1` and `<= 30`; the
    upper bound is a runaway-cost backstop, not a target.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_carousel_images: int = Field(ge=_MIN_IMAGES, le=_MAX_IMAGES)
    max_reel_frames: int = Field(ge=_MIN_IMAGES, le=_MAX_IMAGES)


SINGLE_POST: Final[MultimodalProfile] = MultimodalProfile(max_carousel_images=3, max_reel_frames=3)
"""Default profile for single-post entry points. Preserves pre-config behavior."""

ACCOUNT_SCAN: Final[MultimodalProfile] = MultimodalProfile(max_carousel_images=5, max_reel_frames=5)
"""Default profile for `--scan-account` and account-discovery ticks — roundup-friendly.

Tuned to 5/5 for demo-friendly latency — a 5-slide budget lets the LLM
identify multi-event roundups quickly without burning a full 20-slide
pass. Partial-save discipline (issue #134) means even 5 slides worth of
enumerated events land as `save_event` calls, so the demo shows real
extraction wins without waiting on a heavy pass. For production-grade
coverage of 25-30 slide agendas, raise per-account via
`AccountConfig.max_carousel_images` in `data/sources.yaml`.
"""


def resolve_profile(
    base: MultimodalProfile,
    *,
    max_carousel_images: int | None = None,
    max_reel_frames: int | None = None,
) -> MultimodalProfile:
    """Layer optional per-account overrides on top of a base preset.

    A `None` override inherits the base field. Non-`None` overrides are
    re-validated by `MultimodalProfile` — an out-of-bounds override
    raises `pydantic.ValidationError` here, not silently later inside
    `_build_multimodal_hook`.
    """
    return MultimodalProfile(
        max_carousel_images=(
            max_carousel_images if max_carousel_images is not None else base.max_carousel_images
        ),
        max_reel_frames=(max_reel_frames if max_reel_frames is not None else base.max_reel_frames),
    )


__all__ = [
    "ACCOUNT_SCAN",
    "SINGLE_POST",
    "MultimodalProfile",
    "resolve_profile",
]
