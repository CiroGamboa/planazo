"""Unit tests for the `planazo-sources-instagram` CLI.

Two exercised modes: `--dry-run` (no network — prints one line per
`(account, media_type)` pair the config resolves to) and `--url` (fetches
one post through an injected fake client and prints its JSON). The
live fetch path (real Instagram) is covered by the opt-in live test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from planazo.sources.instagram.cli import main
from planazo.sources.instagram.client import InstagramClient
from planazo.sources.instagram.model_view import InstaloaderPostView


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  instagram:
    default_cadence: "6h"
    default_media_types:
      static_posts: true
      reels: true
      carousels: true
      video_posts: true
    accounts:
      - url: "https://instagram.com/venue_a"
      - url: "https://instagram.com/venue_b"
        media_types:
          static_posts: true
          reels: false
          carousels: true
          video_posts: false
""".strip(),
        encoding="utf-8",
    )
    return path


def test_dry_run_prints_one_line_per_account_media_type_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)

    exit_code = main(["--config", str(config_path), "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    # venue_a: all four media types enabled by default → 4 lines
    # venue_b: static_posts + carousels enabled → 2 lines
    assert len(lines) == 6
    assert "https://instagram.com/venue_a static_posts" in lines[0]
    assert "cadence=" in lines[0]
    venue_b_lines = [line for line in lines if "venue_b" in line]
    assert len(venue_b_lines) == 2
    assert any("static_posts" in line for line in venue_b_lines)
    assert any("carousels" in line for line in venue_b_lines)
    assert not any("reels" in line for line in venue_b_lines)


def test_dry_run_exits_nonzero_when_instagram_source_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("sources: {}\n", encoding="utf-8")

    exit_code = main(["--config", str(path), "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "instagram" in captured.err


class _CannedClient(InstagramClient):
    """`InstagramClient` subclass that skips network calls.

    Instantiated by the CLI via its `client_factory` injection point;
    `fetch_metadata` returns a canned static-post view regardless of the
    requested shortcode, and `load_session_from_env` is a no-op so the test
    does not touch env vars.
    """

    _CANNED_VIEW = InstaloaderPostView.model_validate(
        {
            "shortcode": "CANNED",
            "typename": "GraphImage",
            "caption": "a canned post",
            "date_utc": datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
            "owner_username": "test_venue",
            "url": "https://scontent.cdninstagram.com/canned.jpg",
            "video_url": None,
            "video_duration": None,
            "mediacount": 1,
            "sidecar_nodes": [],
        }
    )

    def __init__(self) -> None:
        # Deliberate no-super: the real `InstagramClient.__init__` would
        # instantiate `instaloader.Instaloader()`, which we skip so tests
        # do not touch the third-party surface.
        pass

    def load_session_from_env(self) -> None:
        return None

    def fetch_metadata(self, shortcode: str) -> InstaloaderPostView:
        return self._CANNED_VIEW


def test_url_flag_fetches_and_prints_one_json_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    config_path = _write_config(tmp_path)

    exit_code = main(
        ["--config", str(config_path), "--url", "https://instagram.com/p/CANNED/"],
        client_factory=_CannedClient,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["source"] == "instagram"
    assert parsed["permalink"] == "https://instagram.com/p/CANNED/"
    assert parsed["caption"] == "a canned post"
    assert parsed["media"][0]["kind"] == "image"


def test_no_mode_flag_exits_with_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)

    exit_code = main(["--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--dry-run" in captured.err
    assert "--url" in captured.err
