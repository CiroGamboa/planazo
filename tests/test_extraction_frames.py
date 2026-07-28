"""Unit tests for ``planazo.extraction.frames.extract_reel_frames``.

Locks the Path A helper's contract from ADR 0013: fixed frame count via
``MAX_REEL_FRAMES``, evenly-spaced timestamps inside the video's duration,
JPEG bytes on happy path, ``FrameExtractionError`` (with a caplog WARNING
naming the URL) on any download or ffmpeg-side failure.

Tests use the bundled ``tests/data/sample_5s.mp4`` fixture — a 5s
``testsrc`` clip generated one-off with the recipe recorded in
``tests/data/README.md``. Fixture is ~26 KB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from planazo.extraction.frames import (
    MAX_REEL_FRAMES,
    FrameExtractionError,
    extract_reel_frames,
)

_FIXTURE_MP4 = Path(__file__).resolve().parent / "data" / "sample_5s.mp4"
_FIXTURE_DURATION_SECONDS = 5.0
_FRAMES_LOGGER = "planazo.extraction.frames"
_TEST_URL = "https://cdn.example/reel.mp4"


class _StubResponse:
    """Minimal ``httpx.Response`` stand-in for the stubbed ``httpx.get``.

    Carries the fixture bytes on ``.content`` and no-ops
    ``raise_for_status``; that's the subset of the response surface the
    helper exercises.
    """

    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _patch_httpx_success(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    def _stub_get(url: str, **kwargs: Any) -> _StubResponse:
        return _StubResponse(payload)

    monkeypatch.setattr(httpx, "get", _stub_get)


def test_extract_reel_frames_returns_jpegs_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    frames = extract_reel_frames(_TEST_URL)

    assert len(frames) == MAX_REEL_FRAMES
    for _, jpeg_bytes in frames:
        assert jpeg_bytes.startswith(b"\xff\xd8"), "must start with JPEG SOI"
        assert len(jpeg_bytes) > 0


def test_extract_reel_frames_defaults_to_max_reel_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    frames = extract_reel_frames(_TEST_URL)

    assert len(frames) == MAX_REEL_FRAMES


def test_extract_reel_frames_frame_count_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    frames = extract_reel_frames(_TEST_URL, frame_count=1)

    assert len(frames) == 1


def test_extract_reel_frames_frame_count_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    frames = extract_reel_frames(_TEST_URL, frame_count=5)

    assert len(frames) == 5


def test_extract_reel_frames_timestamps_strictly_increasing_and_in_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    frames = extract_reel_frames(_TEST_URL, frame_count=MAX_REEL_FRAMES)

    timestamps = [t for t, _ in frames]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert all(0.0 < t < _FIXTURE_DURATION_SECONDS for t in timestamps)


def test_extract_reel_frames_download_failure_raises_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _stub_get(url: str, **kwargs: Any) -> _StubResponse:
        raise httpx.ConnectError(f"unreachable {url}")

    monkeypatch.setattr(httpx, "get", _stub_get)
    caplog.set_level(logging.WARNING, logger=_FRAMES_LOGGER)

    with pytest.raises(FrameExtractionError):
        extract_reel_frames(_TEST_URL)

    warnings = [r for r in caplog.records if r.name == _FRAMES_LOGGER]
    assert warnings, "expected a WARNING record from planazo.extraction.frames"
    assert any(_TEST_URL in r.getMessage() for r in warnings)


def test_extract_reel_frames_probe_failure_raises_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_httpx_success(monkeypatch, b"not a real mp4, this will fail ffprobe")
    caplog.set_level(logging.WARNING, logger=_FRAMES_LOGGER)

    with pytest.raises(FrameExtractionError):
        extract_reel_frames(_TEST_URL)

    warnings = [r for r in caplog.records if r.name == _FRAMES_LOGGER]
    assert warnings, "expected a WARNING record from planazo.extraction.frames"
    assert any(_TEST_URL in r.getMessage() for r in warnings)
