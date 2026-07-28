"""Planazo's Extraction Agent — Instagram post → `Event` via `extract_once`.

Peer of `event_agent.py` (the Recommender). Composition root of the
`extraction/` bounded context: assembles a three-tool registry
(`fetch_instagram_post`, `save_event`, `report_extraction_status`), a
byte-verbatim delegation brief read at import time from
`docs/MVP-ARCHITECTURE.md`, and a multimodal `on_tool_output` hook that
feeds visual context to the LLM as `input_image` messages. Selection per
media shape:

- ``GraphImage`` — one ``input_image`` for the sole image asset.
- ``GraphSidecar`` (carousel) — up to ``MAX_CAROUSEL_IMAGES`` slides, each
  prefixed by a ``"Slide i/K"`` text part.
- ``GraphVideo`` (reel) — the hook downloads the reel ``video_url``,
  extracts ``MAX_REEL_FRAMES`` evenly-spaced JPEG frames via ``ffmpeg``,
  and sends them as base64 ``input_image`` data-URLs alongside the
  thumbnail cover frame. Silently degrades to the thumbnail-only cover
  frame on :class:`FrameExtractionError`; one ``logger.warning`` line is
  the operator-facing signal for the degrade branch. Multi-video sidecars
  (``n_videos >= 2``) route to the thumbnail-only arm without invoking
  the frame helper — they are slideshows, not single reels.

The single fixed user prompt is `USER_MESSAGE`: extractors take no user-
composed message. The URL rides the system prompt (delegation brief + rules
+ URL) so the LLM cannot confuse "the post to extract" with a caption's
attempt to redirect the run.

Terminal state is a code-shape guarantee (ADR 0005, decision 5): the LLM
ends a run by calling exactly one of `save_event` (success) or
`report_extraction_status` (unhappy). `extract_once` inspects the trace's
tool calls to determine the terminal state — never JSON-parses
`LoopResult.answer`.

`save_event` runs without an `ApprovalGate` in the Extractor: `save_event`
is not in `IRREVERSIBLE_TOOLS` (ADR 0002 + ADR 0005 decision 4).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, cast, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentlib.core import STRONG
from planazo.agents.loop import LoopResult, StepRecord, run_loop
from planazo.catalog import ExtractionRunIndexEntry, record_extraction_run, save_event
from planazo.catalog.models import Event
from planazo.extraction.audit import ExtractionRunLogger
from planazo.extraction.frames import MAX_REEL_FRAMES, FrameExtractionError, extract_reel_frames
from planazo.extraction.models import (
    ExtractionErrorType,
    ExtractionResult,
    ExtractionStatus,
)
from planazo.memory.rules import load_rules
from planazo.sources.config import load_config
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClient
from planazo.sources.instagram.tools import build_fetch_instagram_post
from planazo.storage import db
from tools.schema import schema_for

logger = logging.getLogger(__name__)


def _read_delegation_brief() -> str:
    """Read the anchor-bracketed brief text from `docs/MVP-ARCHITECTURE.md`.

    The block is bracketed by two HTML comment anchors — a rewrite of the
    brief in MVP-ARCH lands in the same commit as any change to
    `DELEGATION_BRIEF`, so future edits stay locked byte-for-byte. Anchor
    scanning is used instead of heading-rank parsing so future heading
    additions cannot silently truncate the constant.
    """
    doc_path = Path(__file__).resolve().parents[3] / "docs" / "MVP-ARCHITECTURE.md"
    text = doc_path.read_text(encoding="utf-8")
    start_marker = "<!-- extraction-delegation-brief:start -->"
    end_marker = "<!-- extraction-delegation-brief:end -->"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker)
    return text[start:end].strip("\n")


DELEGATION_BRIEF: Final[str] = _read_delegation_brief()

USER_MESSAGE: Final[str] = "Extract every distinct event announced by the Instagram post above."
MAX_STEPS: Final[int] = 8
MAX_OUTPUT_TOKENS: Final[int] = 2000
MAX_CAROUSEL_IMAGES: Final[int] = 3


class _ReportedStatus(BaseModel):
    """Boundary-validated payload of one `report_extraction_status` call.

    Same Literals as `ExtractionResult` (excluding the source-adapter
    passthrough branches the terminal LLM tool never signals directly) and
    the same 200-char cap on `notes`. Validation failure surfaces as an
    `invalid_reported_status` typed error dict the LLM sees on its next
    turn.
    """

    model_config = ConfigDict(extra="forbid")

    status: ExtractionStatus
    error_type: ExtractionErrorType
    notes: str = Field(default="", max_length=200)


def report_extraction_status(status: str, error_type: str, notes: str = "") -> dict[str, object]:
    """Terminal LLM tool for every non-success Extractor branch.

    Call this to end an extraction run that did not persist a new `Event`.
    Pass `status` = `"needs_clarification"` when the post is ambiguous
    (missing date, out-of-metro venue, multi-event carousel) and
    `status` = `"error"` when the run cannot recover (rate-limited, auth
    failure, low-confidence extraction). `notes` is capped at 200 characters
    for operator-facing diagnostics only — never quote or paraphrase the
    post's caption; `notes` is not for repeating scraped content.
    """
    try:
        validated = _ReportedStatus.model_validate(
            {"status": status, "error_type": error_type, "notes": notes}
        )
    except ValidationError as exc:
        return {"error_type": "invalid_reported_status", "message": str(exc)}
    return {
        "reported": True,
        "status": validated.status,
        "error_type": validated.error_type,
        "notes": validated.notes,
    }


def _build_multimodal_hook(
    url: str,
) -> Callable[[StepRecord], list[dict[str, Any]] | None]:
    """Return the `on_tool_output` hook closured over the extraction target `url`.

    Selection is driven by the counts of ``kind == "image"`` and
    ``kind == "video"`` assets in ``record.result["media"]``, keeping the
    hook domain-model-free:

    - ``n_images >= 2`` — carousel branch. Emits one user message with
      interleaved ``input_text`` + ``input_image`` parts for the first
      ``MAX_CAROUSEL_IMAGES`` image assets (in media-list order), each
      slide prefixed by ``"Slide {i}/{k} from the fetched post — {url}:"``.
    - ``n_images == 1`` — single-image branch. One ``input_text`` + one
      ``input_image`` for the sole image asset.
    - ``n_images == 0`` and ``n_videos == 1`` — reel branch. Downloads the
      video URL, extracts ``MAX_REEL_FRAMES`` evenly-spaced JPEG frames
      via :func:`planazo.extraction.frames.extract_reel_frames`, and emits
      one user message with an envelope ``input_text`` prefix, one
      ``(text, input_image)`` pair per frame (frames sent as base64
      ``data:image/jpeg;base64,...`` data-URLs), and a trailing
      ``(text, input_image)`` pair for the thumbnail cover frame when
      present. On :class:`FrameExtractionError` the hook logs one
      ``WARNING`` record and falls through to the thumbnail-only arm
      below.
    - ``n_images == 0`` and any other video count (0 or ``>= 2``) — a
      ``kind == "thumbnail"`` asset present drives the thumbnail-only
      fallback (byte-identical to the M3 shape). Multi-video sidecars
      (``n_videos >= 2``) route through this arm — they are slideshows,
      not single reels; the frame helper is not invoked.
    - Otherwise — an ``input_text``-only "no visual asset available for
      this post" fallback.

    Video assets are never sent as ``input_image`` parts inside the
    carousel branch — the image-count branches fire first. See ADR 0005
    §D7 (partially superseded by M3.5 #65 for carousels) and ADR 0013 for
    the reel-frame boundary shift.
    """

    def _multimodal_hook(record: StepRecord) -> list[dict[str, Any]] | None:
        if record.tool != "fetch_instagram_post":
            return None
        result = record.result
        # Error-branch guards — state them first so a source-adapter typed
        # error dict is never fed through the media selection logic.
        if not isinstance(result, dict):
            return None
        if "error_type" in result:
            return None
        if "media" not in result:
            return None
        media = result["media"]
        if not isinstance(media, list) or not media:
            return [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "no visual asset available for this post",
                        }
                    ],
                }
            ]
        image_assets: list[dict[str, Any]] = [
            asset for asset in media if isinstance(asset, dict) and asset.get("kind") == "image"
        ]
        if len(image_assets) >= 2:
            k = min(len(image_assets), MAX_CAROUSEL_IMAGES)
            content: list[dict[str, Any]] = []
            for i, asset in enumerate(image_assets[:k], start=1):
                content.append(
                    {
                        "type": "input_text",
                        "text": f"Slide {i}/{k} from the fetched post — {url}:",
                    }
                )
                content.append({"type": "input_image", "image_url": asset.get("url", "")})
            return [{"role": "user", "content": content}]
        if len(image_assets) == 1:
            selected = image_assets[0]
            return [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (f"Image content from the fetched post — {url} (kind=image):"),
                        },
                        {"type": "input_image", "image_url": selected.get("url", "")},
                    ],
                }
            ]
        thumbnail: dict[str, Any] | None = None
        for asset in media:
            if isinstance(asset, dict) and asset.get("kind") == "thumbnail":
                thumbnail = asset
                break
        video_assets: list[dict[str, Any]] = [
            asset for asset in media if isinstance(asset, dict) and asset.get("kind") == "video"
        ]
        # Reel branch — exactly one video asset (n_images == 0 already
        # implied by falling through the image-count branches above). Multi-
        # video sidecars fall through unchanged to the thumbnail arm below,
        # preserving the M3 fallback shape.
        if len(video_assets) == 1:
            video_asset = video_assets[0]
            video_url = str(video_asset.get("url", ""))
            try:
                frames = extract_reel_frames(video_url, frame_count=MAX_REEL_FRAMES)
            except FrameExtractionError as exc:
                logger.warning("reel frame extraction failed for url=%s: %s", video_url, exc)
            else:
                n = len(frames)
                thumb_clause = (
                    ", followed by the thumbnail cover frame" if thumbnail is not None else ""
                )
                envelope = (
                    f"Reel content from the fetched post — {url}. "
                    f"{n} evenly-spaced frames extracted from the video{thumb_clause}:"
                )
                content = [{"type": "input_text", "text": envelope}]
                for i, (timestamp, jpeg_bytes) in enumerate(frames, start=1):
                    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                    content.append(
                        {
                            "type": "input_text",
                            "text": f"Frame {i}/{n} at t≈{timestamp:.1f}s:",
                        }
                    )
                    content.append(
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
                    )
                if thumbnail is not None:
                    content.append(
                        {
                            "type": "input_text",
                            "text": f"Thumbnail cover frame from the fetched post — {url}:",
                        }
                    )
                    content.append(
                        {"type": "input_image", "image_url": str(thumbnail.get("url", ""))}
                    )
                return [{"role": "user", "content": content}]
        if thumbnail is not None:
            return [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Image content from the fetched post — {url} (kind=thumbnail):"
                            ),
                        },
                        {"type": "input_image", "image_url": thumbnail.get("url", "")},
                    ],
                }
            ]
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "no visual asset available for this post",
                    }
                ],
            }
        ]

    return _multimodal_hook


def _truncate_notes(text: str) -> str:
    """Cap a free-form LLM string to the `ExtractionResult.notes` max length."""
    if len(text) <= 200:
        return text
    return text[:200]


def _default_source() -> InstagramSource:
    client = InstagramClient()
    client.load_session_from_env()
    return InstagramSource(load_config().sources["instagram"], client)


def extract_once(
    url: str,
    delegator_user_id: int,
    *,
    source: InstagramSource | None = None,
    model: str = STRONG,
) -> ExtractionResult:
    """Run one Instagram-post → `Event` extraction and return the hand-off.

    Delegated by the Recommender through the `dispatch_extraction` tool
    (`extraction.tools.build_dispatch_extraction`). Not user-facing
    directly: `url` and `delegator_user_id` come from the Recommender's
    session; the caption text never crosses back across the return.
    """
    run_id = str(uuid4())
    resolved_source = source if source is not None else _default_source()

    conn = db.connect()
    try:
        record_extraction_run(
            conn,
            ExtractionRunIndexEntry(run_id=run_id, user_id=delegator_user_id, url=url),
        )
    finally:
        conn.close()

    fetch_schema, fetch_callable = build_fetch_instagram_post(resolved_source)
    tool_schemas: list[dict[str, Any]] = [
        fetch_schema,
        schema_for(save_event),
        schema_for(report_extraction_status),
    ]
    registry: dict[str, Any] = {
        "fetch_instagram_post": fetch_callable,
        "save_event": save_event,
        "report_extraction_status": report_extraction_status,
    }

    system_text = f"{load_rules()}\n\n{DELEGATION_BRIEF}\n\nURL to extract: {url}"

    logger = ExtractionRunLogger(
        run_id=run_id,
        url=url,
        delegator_user_id=delegator_user_id,
        user_message=USER_MESSAGE,
        model=model,
    )

    trace: list[StepRecord] = []

    def observe(record: StepRecord) -> None:
        trace.append(record)
        logger(record)

    loop_result = run_loop(
        user_message=USER_MESSAGE,
        tools=tool_schemas,
        registry=registry,
        model=model,
        max_steps=MAX_STEPS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        on_step=observe,
        on_tool_output=_build_multimodal_hook(url),
        system=system_text,
    )
    logger.complete(loop_result)

    return _build_result(trace, loop_result)


def _build_result(trace: list[StepRecord], loop_result: LoopResult) -> ExtractionResult:
    """Inspect the trace to decide the terminal state and shape the hand-off."""
    save_event_records = [rec for rec in trace if rec.tool == "save_event"]
    report_records = [rec for rec in trace if rec.tool == "report_extraction_status"]
    fetch_records = [rec for rec in trace if rec.tool == "fetch_instagram_post"]

    # Partition `save_event` returns into (successes → Event) and (failures →
    # error-dict). Preserve trace order for both — carousel slot indices ride
    # in the `Event` payload, so the returned `events` list mirrors the LLM's
    # call sequence.
    events: list[Event] = []
    failures: list[dict[str, object]] = []
    for record in save_event_records:
        result = record.result
        if not isinstance(result, dict):
            continue
        saved = result.get("saved")
        if isinstance(saved, dict):
            events.append(Event.model_validate(saved))
        elif "error_type" in result:
            failures.append(result)

    # Happy path — at least one `save_event` persisted an `Event`. Any success
    # wins over any failure (the M3 "get any event you can" bias, now
    # generalised to N events). `notes` uses a redacted `[error_type: <token>]`
    # construction on the mixed-success-plus-failure branch to close the Rule 2
    # leak channel (a Pydantic `ValidationError.__str__` echoes submitted field
    # values, and `title` may carry caption bytes).
    if events:
        first_reported: dict[str, object] | None = None
        for record in reversed(report_records):
            result = record.result
            if not isinstance(result, dict):
                continue
            if "error_type" in result and not result.get("reported"):
                # `_ReportedStatus` rejected the args — the LLM saw an
                # `invalid_reported_status` typed error and the run continued.
                continue
            reported_status = result.get("status")
            reported_error_type = result.get("error_type")
            if isinstance(reported_status, str) and isinstance(reported_error_type, str):
                if reported_status == "ok":
                    # `report_extraction_status(status="ok")` is a caller bug;
                    # ignore it and keep looking for a real unhappy report.
                    continue
                first_reported = result
                break

        if failures:
            first_failure_type = str(failures[0].get("error_type", ""))
            notes = _truncate_notes(
                f"{len(events)} saved; {len(failures)} save_event failure(s)"
                f" [error_type: {first_failure_type}]"
            )
        elif first_reported is not None:
            reported_error_type = str(first_reported.get("error_type", ""))
            reported_notes = str(first_reported.get("notes", ""))
            notes = _truncate_notes(
                f"{len(events)} saved; LLM also reported {reported_error_type}: {reported_notes}"
            )
        else:
            notes = _truncate_notes(loop_result.answer or "")

        return ExtractionResult(
            status="ok",
            events=events,
            error_type=None,
            notes=notes,
        )

    # All-failed with no successes — the same redacted `[error_type: <token>]`
    # construction as the mixed branch above. M3 shipped raw ValidationError
    # strings here; this stage closes that pre-existing Rule 2 leak channel in
    # the same commit that would otherwise widen it to the partial-success
    # path.
    if failures:
        first_failure_type = str(failures[0].get("error_type", ""))
        notes = _truncate_notes(
            f"{len(failures)} save_event failure(s) [error_type: {first_failure_type}]"
        )
        return ExtractionResult(
            status="error",
            error_type="save_event_failed",
            notes=notes,
        )

    # Unhappy terminal call — `report_extraction_status`.
    for record in reversed(report_records):
        result = record.result
        if not isinstance(result, dict):
            continue
        if "error_type" in result and not result.get("reported"):
            # `_ReportedStatus` rejected the args; the LLM saw an
            # `invalid_reported_status` typed error and the run continued.
            continue
        reported_status = result.get("status")
        reported_error_type = result.get("error_type")
        reported_notes = result.get("notes", "")
        if isinstance(reported_status, str) and isinstance(reported_error_type, str):
            status_cast = cast(ExtractionStatus, reported_status)
            error_type_cast = cast(ExtractionErrorType, reported_error_type)
            notes_cast = _truncate_notes(str(reported_notes))
            if status_cast == "ok":
                # `report_extraction_status` is not the success terminal — a
                # status="ok" call is a caller bug; fall through to the
                # low-confidence bucket.
                continue
            return ExtractionResult(
                status=status_cast,
                error_type=error_type_cast,
                notes=notes_cast,
            )

    # Source-adapter typed error and no terminal tool call — surface the
    # first fetch's error branch (usually the only one before the LLM gives up).
    # If the source-adapter taxonomy ever grows a branch outside
    # `ExtractionErrorType` (ADR 0006 → ADR 0005), degrade to
    # `low_confidence_extraction` rather than raising `ValidationError` at
    # ExtractionResult construction — the unknown branch's name still lands
    # in `notes` for the operator + monitor.
    _known_error_types = set(get_args(ExtractionErrorType))
    for record in fetch_records:
        result = record.result
        if isinstance(result, dict) and "error_type" in result:
            error_type_str = cast(str, result["error_type"])
            message = str(result.get("message", ""))
            if error_type_str in _known_error_types:
                error_type_cast = cast(ExtractionErrorType, error_type_str)
                return ExtractionResult(
                    status="error",
                    error_type=error_type_cast,
                    notes=_truncate_notes(message),
                )
            degraded_notes = _truncate_notes(f"unknown source error {error_type_str!r}: {message}")
            return ExtractionResult(
                status="error",
                error_type="low_confidence_extraction",
                notes=degraded_notes,
            )

    # Budget cap or unexplained stop — the loop ended without a terminal call.
    return ExtractionResult(
        status="error",
        error_type="low_confidence_extraction",
        notes="ran out of steps without a terminal call",
    )


__all__ = [
    "DELEGATION_BRIEF",
    "MAX_CAROUSEL_IMAGES",
    "MAX_OUTPUT_TOKENS",
    "MAX_STEPS",
    "USER_MESSAGE",
    "extract_once",
    "report_extraction_status",
]
