from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.sources.config import (
    AccountConfig,
    MediaTypeFlags,
    SourceConfig,
    SourcesConfig,
    load_config,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_parses_shipped_schema_example() -> None:
    config = load_config(Path("data/sources.yaml"))

    assert "instagram" in config.sources
    instagram = config.sources["instagram"]
    assert instagram.default_cadence == timedelta(hours=6)
    assert instagram.default_media_types == MediaTypeFlags()
    assert len(instagram.accounts) == 2


def test_load_config_folds_per_account_cadence_override(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "sources.yaml",
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
        cadence: "24h"
      - url: "https://instagram.com/venue_b"
""".strip(),
    )

    config = load_config(yaml_path)
    instagram = config.sources["instagram"]

    override, default = instagram.accounts
    assert override.resolved_cadence(instagram) == timedelta(hours=24)
    assert default.resolved_cadence(instagram) == timedelta(hours=6)


def test_account_resolved_media_types_folds_override() -> None:
    instagram = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        accounts=[
            AccountConfig(
                url="https://instagram.com/no_reels_venue",
                media_types=MediaTypeFlags(reels=False),
            ),
            AccountConfig(url="https://instagram.com/defaults_venue"),
        ],
    )

    override, default = instagram.accounts
    assert override.resolved_media_types(instagram).reels is False
    assert override.resolved_media_types(instagram).static_posts is True
    assert default.resolved_media_types(instagram) == MediaTypeFlags()


def test_load_config_rejects_malformed_cadence_string(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "sources.yaml",
        """
sources:
  instagram:
    default_cadence: "6 hours"
    default_media_types:
      static_posts: true
      reels: true
      carousels: true
      video_posts: true
    accounts: []
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_load_config_rejects_unknown_media_type_flag(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "sources.yaml",
        """
sources:
  instagram:
    default_cadence: "6h"
    default_media_types:
      static_posts: true
      reels: true
      carousels: true
      video_posts: true
      podcasts: true
    accounts: []
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_load_config_rejects_missing_required_field(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "sources.yaml",
        """
sources:
  instagram:
    default_media_types:
      static_posts: true
      reels: true
      carousels: true
      video_posts: true
    accounts: []
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_sources_config_defaults_to_empty_map() -> None:
    assert SourcesConfig().sources == {}
