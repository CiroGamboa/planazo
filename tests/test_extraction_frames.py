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
import tempfile
from pathlib import Path
from typing import Any

import ffmpeg  # type: ignore[import-untyped]
import httpx
import pytest

from planazo.extraction import frames as frames_module
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


def test_extract_reel_frames_ffmpeg_binary_absent_raises_frame_extraction_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing ``ffmpeg`` / ``ffprobe`` on ``PATH`` surfaces as ``FrameExtractionError``.

    Regression gate for the reviewer-fix widening of the probe-stage
    ``except`` clause to ``(ffmpeg.Error, FileNotFoundError)``. Under the
    pre-fix code the raw ``FileNotFoundError`` from ``subprocess.Popen``
    escapes past ``ffmpeg.Error`` and the extractor hook's silent-degrade
    guard misses it.
    """
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    def _raise_fnf(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'ffprobe'")

    monkeypatch.setattr(frames_module.ffmpeg, "probe", _raise_fnf)
    caplog.set_level(logging.WARNING, logger=_FRAMES_LOGGER)

    with pytest.raises(FrameExtractionError) as excinfo:
        extract_reel_frames(_TEST_URL)

    assert "ffprobe" in str(excinfo.value)
    warnings = [r for r in caplog.records if r.name == _FRAMES_LOGGER]
    assert warnings, "expected a WARNING record from planazo.extraction.frames"
    assert any(_TEST_URL in r.getMessage() for r in warnings)
    assert any("ffprobe" in r.getMessage() for r in warnings)


def test_extract_reel_frames_ffmpeg_error_raises_frame_extraction_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Mid-extract ``ffmpeg.Error`` (probe succeeds, ``.run()`` fails) maps to typed error.

    Locks the second ``except`` branch inside ``_extract``, which the
    probe-failure test above does not reach. The stream builder is
    replaced with a stub whose ``.run()`` raises ``ffmpeg.Error``; the
    top-level probe call is stubbed to return a valid duration.
    """
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    def _stub_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"format": {"duration": "5.0"}}

    class _StubStream:
        def output(self, *args: Any, **kwargs: Any) -> _StubStream:
            return self

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise ffmpeg.Error("ffmpeg", stdout=b"", stderr=b"conversion failed")

    def _stub_input(*args: Any, **kwargs: Any) -> _StubStream:
        return _StubStream()

    monkeypatch.setattr(frames_module.ffmpeg, "probe", _stub_probe)
    monkeypatch.setattr(frames_module.ffmpeg, "input", _stub_input)
    caplog.set_level(logging.WARNING, logger=_FRAMES_LOGGER)

    with pytest.raises(FrameExtractionError) as excinfo:
        extract_reel_frames(_TEST_URL)

    assert "ffmpeg extract failed" in str(excinfo.value)
    warnings = [r for r in caplog.records if r.name == _FRAMES_LOGGER]
    assert warnings, "expected a WARNING record from planazo.extraction.frames"
    assert any(_TEST_URL in r.getMessage() for r in warnings)


def test_extract_reel_frames_cleans_up_tempdir_on_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tempfile.TemporaryDirectory`` is cleaned up when the extract stage raises.

    Records the tempdir path via a spy on ``tempfile.TemporaryDirectory``
    and asserts the directory is gone from disk after
    ``FrameExtractionError`` unwinds — the context-manager cleanup path
    is load-bearing per ADR 0013's "temp-file lifecycle discipline"
    trade-off.
    """
    _patch_httpx_success(monkeypatch, _FIXTURE_MP4.read_bytes())

    def _stub_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"format": {"duration": "5.0"}}

    class _StubStream:
        def output(self, *args: Any, **kwargs: Any) -> _StubStream:
            return self

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise ffmpeg.Error("ffmpeg", stdout=b"", stderr=b"conversion failed")

    def _stub_input(*args: Any, **kwargs: Any) -> _StubStream:
        return _StubStream()

    monkeypatch.setattr(frames_module.ffmpeg, "probe", _stub_probe)
    monkeypatch.setattr(frames_module.ffmpeg, "input", _stub_input)

    captured_paths: list[str] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    def _spy_temporary_directory(*args: Any, **kwargs: Any) -> Any:
        td = real_temporary_directory(*args, **kwargs)
        captured_paths.append(td.name)
        return td

    monkeypatch.setattr(frames_module.tempfile, "TemporaryDirectory", _spy_temporary_directory)

    with pytest.raises(FrameExtractionError):
        extract_reel_frames(_TEST_URL)

    assert captured_paths, "spy did not capture a tempdir"
    for path in captured_paths:
        assert not Path(path).exists(), f"tempdir {path} leaked past the raise"
