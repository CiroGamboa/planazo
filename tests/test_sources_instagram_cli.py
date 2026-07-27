"""Unit tests for the `planazo-sources-instagram` CLI.

Only `--dry-run` is exercised — the live fetch path is covered by the
opt-in live test. The dry-run mode prints one line per `(account,
media_type)` pair the config resolves to, so the scheduler ticket can
diff the planned fetches without any network activity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from planazo.sources.instagram.cli import main


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
