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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args
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
from planazo.observability import (
    FINAL_ANSWER_CAP,
    RATIONALE_CAP,
    USER_QUERY_CAP,
    AgentRunLogger,
    AgentRunRecord,
    DecisionKind,
    LLMDecision,
    LLMDecisionLogger,
    format_stored_text,
)
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

    # Capture wall-clock boundaries around `run_loop` so the `agent_runs`
    # row's `started_at` / `ended_at` cover the full loop, including every
    # tool dispatch (source fetch, save_event writes) — not just LLM turns.
    started_at = datetime.now(UTC)
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
    ended_at = datetime.now(UTC)
    logger.complete(loop_result)

    # Best-effort SQLite audit-row write. `AgentRunLogger` catches every
    # exception and logs a WARNING; the Extractor's `ExtractionResult` is
    # the primary flow and observability failures must not affect it
    # (Rule 4). No `record_runs` seam on `extract_once` — the JSONL writer
    # above is always on, and the SQLite row-writer matches.
    _record_agent_run_best_effort(
        run_id=run_id,
        user_id=delegator_user_id,
        result=loop_result,
        started_at=started_at,
        ended_at=ended_at,
    )
    # T4 rationale audit — writes AFTER `record_agent_run` because
    # `llm_decisions.run_id` is FK to `agent_runs.run_id`. If the
    # `agent_runs` write failed (T3 swallowed the exception), the
    # `llm_decisions` writes will also fail on FK — both are best-effort
    # by design.
    _record_llm_decisions_best_effort(
        run_id=run_id,
        trace=trace,
        result=loop_result,
        recorded_at=ended_at,
    )

    return _build_result(trace, loop_result)


def _record_agent_run_best_effort(
    *,
    run_id: str,
    user_id: int,
    result: LoopResult,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    """Build the sanitized `AgentRunRecord` and hand it to `AgentRunLogger`.

    `USER_MESSAGE` is the fixed Extractor prompt — the caption itself
    rides the system prompt (ADR 0005 §Trust boundary), never `user_query`,
    so the sanitizer's leak channel is not exercised on the Extractor
    side. `preference_read_error` cannot reach here — that branch is
    Recommender-side only. The assert narrows the Literal-widening hole
    mypy would otherwise flag on `result.stopped`.
    """
    assert result.stopped not in {"preference_read_error", "missing_search_origin"}, (
        "agent_runs records actual loop terminals; pre-run failures must not be logged"
    )
    stopped_literal = cast(Literal["answered", "truncated", "max_steps"], result.stopped)
    agent_logger = AgentRunLogger(conn_factory=db.connect)
    record = AgentRunRecord(
        run_id=run_id,
        agent_kind="extractor",
        user_id=user_id,
        user_query=format_stored_text(USER_MESSAGE, USER_QUERY_CAP),
        final_answer=(
            format_stored_text(result.answer, FINAL_ANSWER_CAP)
            if result.answer is not None
            else None
        ),
        stopped=stopped_literal,
        steps_count=result.steps,
        started_at=started_at,
        ended_at=ended_at,
    )
    agent_logger.record(record)


def _record_llm_decisions_best_effort(
    *,
    run_id: str,
    trace: list[StepRecord],
    result: LoopResult,
    recorded_at: datetime,
) -> None:
    """Emit one `LLMDecision` per terminal LLM tool call plus a loop-terminal row.

    Rationale sourcing per branch:

    - Successful `save_event` — a `save_event` call has no per-call
      LLM rationale (the LLM produces structured tool arguments, no
      free-form reasoning). Rationale is formulaic:
      ``"saved event: <title>"`` truncated to `RATIONALE_CAP`. Sourcing
      from `title` matches the M4 ranker's corpus-analysis shape: a
      cluster on `error_type` for the failure branches, a cluster on
      "what kinds of events land" for the success branch.
    - `report_extraction_status` — rationale is the LLM's own `notes`
      arg, sanitized through `format_stored_text`. This is the primary
      per-decision rationale channel today.
    - Loop-terminal `truncated` / `max_steps` — one synthetic `error`
      row with a formulaic rationale ("loop truncated" or "max steps
      reached"). No structured `error_type` is emitted by the LLM in
      this branch, so we use the loop-terminal string itself as
      `error_type`.

    Best-effort: `LLMDecisionLogger.record_many` swallows every raise
    with a WARNING log line. The Extractor's `ExtractionResult` is the
    primary flow — an FK violation from a missing `agent_runs` row
    (T3 write failed, T4 must fail too), a driver error, a bogus
    trace entry — none propagates out of this helper.
    """
    decisions: list[LLMDecision] = []
    for record in trace:
        if record.tool == "save_event":
            decision = _decision_from_save_event(record, run_id=run_id, recorded_at=recorded_at)
            if decision is not None:
                decisions.append(decision)
        elif record.tool == "report_extraction_status":
            decision = _decision_from_report_status(record, run_id=run_id, recorded_at=recorded_at)
            if decision is not None:
                decisions.append(decision)

    if result.stopped in ("truncated", "max_steps"):
        # Loop-terminal branch — no LLM tool call fired to explain the
        # early stop. Emit one synthetic `error` row so post-hoc trace
        # inspection can distinguish "extractor gave up" from "extractor
        # explicitly refused" — same shape M4's ranker will filter on.
        stopped_str = result.stopped
        raw_rationale = (
            format_stored_text(result.answer, RATIONALE_CAP)
            if result.answer is not None
            else ("loop truncated mid-turn" if stopped_str == "truncated" else "max_steps reached")
        )
        decisions.append(
            LLMDecision(
                run_id=run_id,
                decision_kind="error",
                event_db_id=None,
                error_type=("loop_truncated" if stopped_str == "truncated" else "max_steps"),
                rationale=raw_rationale or "loop terminated early",
                recorded_at=recorded_at,
            )
        )

    LLMDecisionLogger(conn_factory=db.connect).record_many(decisions)


def _decision_from_save_event(
    record: StepRecord, *, run_id: str, recorded_at: datetime
) -> LLMDecision | None:
    """Build the `save_event` `LLMDecision` row, or `None` on non-success.

    A `save_event` return with `error_type` (invalid data, duplicate row,
    an unregistered-tool marker) is NOT persisted as a `save_event`
    LLMDecision — the row would advertise a persisted `Event` that
    never existed. Failures of `save_event` are visible in the JSONL
    sidecar and the caller-side aggregation on `_build_result`; the
    corpus stays clean.

    Rationale is formulaic — `save_event` has no LLM `notes` arg (see
    module docstring). The `title` argument is caption-derived and the
    Rule 2 hook allows it inside the DB subject to sanitization.
    """
    result = record.result
    if not isinstance(result, dict):
        return None
    event_db_id = result.get("event_db_id")
    saved = result.get("saved")
    if not isinstance(saved, dict) or not isinstance(event_db_id, int):
        return None
    title = record.arguments.get("title", "") if isinstance(record.arguments, dict) else ""
    rationale = format_stored_text(f"saved event: {title}", RATIONALE_CAP)
    return LLMDecision(
        run_id=run_id,
        decision_kind="save_event",
        event_db_id=event_db_id,
        error_type=None,
        rationale=rationale or "saved event",
        recorded_at=recorded_at,
    )


def _decision_from_report_status(
    record: StepRecord, *, run_id: str, recorded_at: datetime
) -> LLMDecision | None:
    """Build the `report_extraction_status` `LLMDecision` row, or `None`.

    `_ReportedStatus` rejects invalid args and returns an
    `invalid_reported_status` marker with `"reported": False`. Those
    rejected calls are skipped — they describe an LLM misuse of the
    tool, not a decision. `status="ok"` on `report_extraction_status`
    is also a caller bug (that tool is not the success terminal); we
    skip it too so the corpus does not mis-attribute an `answered`
    row to a non-success decision kind.
    """
    result = record.result
    if not isinstance(result, dict):
        return None
    if not result.get("reported"):
        return None
    status = result.get("status")
    error_type = result.get("error_type")
    notes = result.get("notes", "")
    if not isinstance(status, str) or not isinstance(error_type, str):
        return None
    if status == "ok":
        return None
    if status not in ("needs_clarification", "error"):
        return None
    decision_kind: DecisionKind = (
        "needs_clarification" if status == "needs_clarification" else "error"
    )
    rationale_source = notes if isinstance(notes, str) else ""
    rationale = format_stored_text(rationale_source, RATIONALE_CAP)
    if not rationale:
        # `notes` is optional on `report_extraction_status`; when empty
        # fall back to the error_type token so the audit trail always
        # has *something* other than "" — a rationale field of "" is
        # legally sanitized but useless for post-hoc inspection.
        rationale = format_stored_text(f"reported {status}: {error_type}", RATIONALE_CAP)
    return LLMDecision(
        run_id=run_id,
        decision_kind=decision_kind,
        event_db_id=None,
        error_type=error_type,
        rationale=rationale,
        recorded_at=recorded_at,
    )


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
