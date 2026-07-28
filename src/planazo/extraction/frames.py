"""Reel frame extraction — downloads a video URL and yields JPEG frames.

Records the Path A boundary shift from ADR 0013: Zen's Responses API does
not accept an ``input_video`` content part, so the Extractor's multimodal
hook downloads the reel's ``video_url`` to a temp file, extracts
``MAX_REEL_FRAMES`` evenly-spaced JPEG frames via ``ffmpeg``, and sends
them as base64 ``input_image`` data-URLs alongside the thumbnail cover
frame. The load-bearing knobs are ``MAX_REEL_FRAMES = 3`` (text-on-video
OCR readability against per-run token cost) and mjpeg ``q:v = 5`` — the
ffmpeg mjpeg encoder uses a 2-31 quality scale where lower is higher
quality, not the libjpeg 0-100 scale.

The temp directory lifecycle is scoped to one call via
``tempfile.TemporaryDirectory`` — frames are read into memory before the
context exits so the OS deletes the directory unconditionally on the way
out (success or raise). Any failure between download and last frame read
raises :class:`FrameExtractionError`; the extractor's multimodal hook
catches this and degrades to the thumbnail-only cover-frame message
shape. A single ``logger.warning`` line is emitted with the URL and the
cause before the raise — that record is the operator-facing signal for
the silent-degrade branch.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Final

import ffmpeg  # type: ignore[import-untyped]
import httpx

logger = logging.getLogger(__name__)

MAX_REEL_FRAMES: Final[int] = 3

_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 10.0
_MJPEG_QUALITY: Final[int] = 5


class FrameExtractionError(Exception):
    """Raised when the reel cannot be turned into JPEG frames.

    The extractor's multimodal hook catches this and falls back to the
    thumbnail-only shape. The exception carries a short cause string for
    the operator-facing WARNING log line — the message is what the hook's
    caplog-locked assertions look at.
    """


def extract_reel_frames(
    video_url: str,
    *,
    frame_count: int = MAX_REEL_FRAMES,
) -> list[tuple[float, bytes]]:
    """Download ``video_url`` and return ``frame_count`` evenly-spaced JPEG frames.

    Returns a list of ``(timestamp_seconds, jpeg_bytes)`` tuples in
    timestamp order. Timestamps are ``duration * (i / (frame_count + 1))``
    for ``i`` in ``1..frame_count`` — evenly spaced across the video,
    excluding the endpoints.

    Raises :class:`FrameExtractionError` on any failure: download failure,
    ffprobe / ffmpeg execution failure, missing or empty frame file,
    unexpected probe duration. The exception is the typed-error surface
    for the extractor hook's silent-degrade branch (AGENTS.md Rule 4).
    The tempdir is cleaned up before the raise via the
    :class:`tempfile.TemporaryDirectory` context manager.
    """
    try:
        return _extract(video_url, frame_count=frame_count)
    except FrameExtractionError as exc:
        logger.warning("reel frame extraction failed for url=%s: %s", video_url, exc)
        raise


def _extract(video_url: str, *, frame_count: int) -> list[tuple[float, bytes]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        video_path = tmp_path / "reel.mp4"

        try:
            response = httpx.get(
                video_url,
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FrameExtractionError(f"video download failed: {exc}") from exc

        video_path.write_bytes(response.content)

        # ``FileNotFoundError`` is the specific shape ``ffmpeg-python``
        # surfaces when the host ``ffmpeg`` / ``ffprobe`` binary is absent
        # from ``PATH`` — the wrapper spawns a subprocess and the missing
        # binary raises at ``subprocess.Popen`` before any ``ffmpeg.Error``
        # can be constructed. Catching both keeps the missing-binary case
        # on the typed-error branch instead of leaking ``FileNotFoundError``
        # past the extractor hook's silent-degrade guard.
        try:
            probe: dict[str, Any] = ffmpeg.probe(str(video_path))
        except (ffmpeg.Error, FileNotFoundError) as exc:
            raise FrameExtractionError(f"ffprobe failed: {exc}") from exc

        duration = _read_duration(probe)
        if duration is None or duration <= 0.0:
            raise FrameExtractionError(f"unexpected probe duration: {duration!r}")

        frames: list[tuple[float, bytes]] = []
        for i in range(1, frame_count + 1):
            timestamp = duration * (i / (frame_count + 1))
            frame_path = tmp_path / f"frame_{i}.jpg"
            try:
                (
                    ffmpeg.input(str(video_path), ss=timestamp)
                    .output(str(frame_path), vframes=1, **{"q:v": _MJPEG_QUALITY})
                    .run(quiet=True, overwrite_output=True)
                )
            except (ffmpeg.Error, FileNotFoundError) as exc:
                raise FrameExtractionError(
                    f"ffmpeg extract failed at t={timestamp:.2f}s: {exc}"
                ) from exc

            if not frame_path.exists():
                raise FrameExtractionError(f"ffmpeg produced no frame file at t={timestamp:.2f}s")
            data = frame_path.read_bytes()
            if not data:
                raise FrameExtractionError(
                    f"ffmpeg produced empty frame file at t={timestamp:.2f}s"
                )
            frames.append((timestamp, data))

        return frames


def _read_duration(probe: dict[str, Any]) -> float | None:
    """Pull duration seconds from an ``ffmpeg.probe`` result — best-effort.

    Returns ``None`` on any shape drift; the caller treats that as a
    :class:`FrameExtractionError`.
    """
    format_info = probe.get("format")
    if not isinstance(format_info, dict):
        return None
    raw = format_info.get("duration")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
