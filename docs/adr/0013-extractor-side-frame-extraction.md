# 0013 — Extractor-side reel frame extraction via ffmpeg

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Relates to:** [`0005-multi-agent-shape.md`](0005-multi-agent-shape.md) (multi-agent shape and the Extractor's tool boundary), [`0006-instagram-extraction-approach.md`](0006-instagram-extraction-approach.md) (partially supersedes §Decision 4 — the ADAPTER still emits URL-only `MediaAsset` entries; the boundary shift is that the EXTRACTOR now downloads binary reel content and materializes JPEG frames), [`0012-multi-event-extraction.md`](0012-multi-event-extraction.md) (multi-event carousels; reels are the other half of M3.5's target content), scheduled-ingestion pipeline (issue #68, ADR pending).

## Context

M3 shipped `_multimodal_hook` with a single-image visual context: static posts sent one `input_image`; carousels sent the first image (widened to up to three in #65); reels (`GraphVideo`) sent the cover-frame thumbnail only. Text-on-video reels are the dominant shape for the Barcelona venue-announcement channel targeted by M3.5's scheduler (`@sala_apolo`, `@razzmatazz`, `@bcn.agenda`) — title, date, and venue live on frames the LLM never sees under the M3 shape. The result is `missing_date` / low-confidence extractions on a large fraction of the scheduler's yield.

The natural fix is to send the video content, not just a still frame. Two implementation paths are viable at the Zen provider layer:

- **Path B — passthrough.** Include the reel `video_url` as an `input_video` content part alongside the thumbnail `input_image` on the LLM turn, and let Zen's provider fetch the video server-side. Boundary shape unchanged (`MediaAsset.url` remains URL-only; the Extractor still does no binary download).
- **Path A — extractor-side frame extraction.** Download the reel to a temp file, extract N evenly-spaced JPEG frames via `ffmpeg`, and send them as base64 `input_image` data-URLs alongside the thumbnail. Shifts a binary-download responsibility from "adapter server-side" (Path B) to "extractor host-side" (Path A).

A live probe against Zen determined the choice: three shape variants of `{"type": "input_video", ...}` on the Responses API returned `400 invalid_request_error` naming `input_video` as an unsupported content type. Path B is not viable at this Zen version; Path A is the only path that widens reel context for MVP.

## Decision

Planazo widens the Extractor's multimodal hook so reels are sent to the LLM as three evenly-spaced JPEG frames plus the thumbnail cover frame. The frame-extraction primitive lives in a new pure helper module — `src/planazo/extraction/frames.py::extract_reel_frames` — that downloads the reel `video_url` via `httpx`, calls `ffmpeg.probe` for duration, materializes N frames at `duration * (i / (N + 1))` for `i` in `1..N`, and returns `list[tuple[float, bytes]]` for the hook to base64-wrap into `input_image` data-URLs. The helper owns its own `tempfile.TemporaryDirectory` — the directory is cleaned up unconditionally on success or raise via the context manager. Any failure between download and last frame read raises `FrameExtractionError`; the multimodal hook silently degrades to the thumbnail-only shape and emits a single `logging.warning` record for operator observability. The frame count is fixed at `MAX_REEL_FRAMES = 3`; frames are encoded with mjpeg `q:v=5` — the ffmpeg mjpeg encoder uses a 2-31 quality scale where lower is higher quality (not the libjpeg 0-100 scale). This ADR partially supersedes ADR 0006 §Decision 4 for the Extractor's downstream binary-fetch behaviour: the ADAPTER still emits URL-only `MediaAsset` entries and never downloads binary content; the EXTRACTOR now downloads reel binary for LLM input.

### Alternatives rejected

- **Zen `input_video` passthrough (Path B).** Rejected empirically: the Step 0 probe against `STRONG` returned `400 invalid_request_error` across three shape variants of the `input_video` content part. The API does not accept the type at this Zen version; every reel would silently degrade.
- **Whisper (or equivalent) audio transcription of the reel's audio track.** Rejected for MVP: adds a subprocess or an audio-model dependency for a use case (silent flyer-style reels; music-only announcements) where the yield is uncertain. Deferred as a follow-up ticket — the frame path is the higher-signal source for the current target accounts.
- **Thumbnail-only (M3 status quo).** Rejected as the ticket's failure mode: text-on-video reels lose the date/venue information, which is the entire yield the scheduler is being built to capture.
- **A paid server-side vendor API (frame extraction as a service).** Rejected: adds cost, adds a third-party fragility surface, and is out of MVP scope. `ffmpeg` on the extractor host is free, well-audited, and standard.
- **`ffmpeg-static` (pip wheel bundling the binary).** Rejected: less audited than the system-package binary, adds ~40 MB to the wheel install, and defers a "which ffmpeg" ambiguity to runtime. The system binary requirement is one line in `AGENTS.md` § Setup & Commands.
- **A persistent per-run cache directory for frames.** Rejected: frames are consumed exactly once (one LLM turn) and never re-read. `tempfile.TemporaryDirectory` is the right scope; a persistent cache adds cleanup risk with no upside.
- **PNG frames.** Rejected: lossless encoding is ~4× the JPEG size at no measurable OCR gain for the target content (flyer text, faces).

## Consequences

### Positive

- **Text-on-video reels become extractable.** The scheduler's target accounts (`@sala_apolo`, `@razzmatazz`, `@bcn.agenda`, ...) put date / venue / title on video frames; the LLM now sees three of them per reel.
- **Failure is a typed branch, not a crash.** `FrameExtractionError` is caught by the multimodal hook; the LLM turn falls back to the thumbnail-only shape (M3 behaviour preserved for the fallback arm). AGENTS.md Rule 4 is upheld.
- **Zero code footprint on the adapter side.** `MediaAsset` shape, `RawPost` shape, and the `sources-instagram` Docker service are unchanged. The boundary shift is entirely inside the Extractor's runtime host.
- **Reusable helper.** A future scheduler pipeline that wants to pre-warm frames off the hot path can call `extract_reel_frames` directly — the helper is pure bytes-in / bytes-out with no import of `agents/extractor.py`.

### Negative / accepted trade-offs

- **New system-binary prerequisite.** The extractor's runtime host now needs `ffmpeg` on PATH. Documented in `AGENTS.md` § Setup & Commands (`brew install ffmpeg` on macOS; `apt-get install ffmpeg` on Linux). M3.5 is host-cron mode — no scheduler container to widen — but a future scheduler-container Dockerfile will need `ffmpeg` installed too.
- **New Python dep — `ffmpeg-python>=0.2,<0.3`.** Small wrapper around the `ffmpeg` binary; ships no type stubs, so the `import ffmpeg` line carries a `# type: ignore[import-untyped]`.
- **JPEG quality knob is ffmpeg-native, not libjpeg-native.** `q:v=5` on the mjpeg encoder sits on a 2-31 scale (lower is higher quality), roughly equivalent to libjpeg 85. A future contributor reading the code needs to know this — hence the module docstring + this ADR both call it out explicitly.
- **Reels pay ~3× image-token cost on STRONG.** Three `input_image` parts per reel, at STRONG-tier pricing. The MVP does not gate this behind a per-run budget cap — `MAX_STEPS=8` already bounds the LLM loop and reels are one-per-fetch.
- **Temp-file lifecycle discipline.** The helper must not leak `TemporaryDirectory` state across a raise. The context-manager idiom handles this; the ADR calls it out as load-bearing so a future refactor doesn't accidentally split the cleanup out of the exception path.

### Follow-ups

- **Wire `_multimodal_hook` to the helper.** Path A Stage 2 — the reel branch on `n_images == 0 and n_videos == 1` calls `extract_reel_frames` and builds the multi-image LLM message. Flips this ADR to Accepted; marks ADR 0006 §D4 as partially superseded.
- **Docs sweep of `docs/MVP-ARCHITECTURE.md`.** Path A Stage 3 — the anchored `DELEGATION_BRIEF` effort-budget bullet, the Mermaid delegation-flow note, the Risks-and-open-questions Multimodal-cost bullet, and §5 Sources's local-setup mention of `ffmpeg`.
- **`refresh(shortcode)` for expired CDN URLs.** Already named as a deferred follow-up in ADR 0006 — a signed-URL expiry between adapter fetch and Extractor download surfaces here as a `FrameExtractionError` with an HTTP-status cause. The graceful-degrade branch handles it; the refresh follow-up is the eventual clean fix.
- **Whisper transcription of the reel audio track.** Filed at merge as a separate ticket — audio-only reels are the residual failure mode after this ADR lands.
- **`ffmpeg` in the future scheduler-container Dockerfile.** Filed at merge — M3.5 runs host-cron and needs no container change; when a scheduler container is introduced (ADR pending, issue #68), its Dockerfile installs `ffmpeg` (`apt-get install -y ffmpeg`, ~50 MB image growth).
