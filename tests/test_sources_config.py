from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.sources.config import (
    AccountConfig,
    MediaTypeFlags,
    PostConfig,
    SourceConfig,
    SourcesConfig,
    enumerate_configured_posts,
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
    # Shipped example: 2 placeholder single-venue accounts + 1 curator
    # roundup entry (`@planesenbarcelona`) with `max_carousel_images: 25`.
    # See issue #134 for the load-bearing rationale.
    assert len(instagram.accounts) == 3
    assert len(instagram.posts) == 2


def test_shipped_schema_carries_planesenbarcelona_override() -> None:
    """Locks the seed override from PR #134 — the demo-ready curator entry
    with `max_carousel_images: 25`. Guard so a future YAML edit that drops
    the override silently reverts the demo's end-to-end story."""
    config = load_config(Path("data/sources.yaml"))
    instagram = config.sources["instagram"]

    matches = [account for account in instagram.accounts if "planesenbarcelona" in account.url]
    assert len(matches) == 1
    assert matches[0].max_carousel_images == 25


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


def test_media_type_flags_enabled_kinds_returns_all_in_declaration_order() -> None:
    flags = MediaTypeFlags()
    assert flags.enabled_kinds() == ["static_posts", "reels", "carousels", "video_posts"]


def test_media_type_flags_enabled_kinds_skips_disabled() -> None:
    flags = MediaTypeFlags(static_posts=True, reels=False, carousels=True, video_posts=False)
    assert flags.enabled_kinds() == ["static_posts", "carousels"]


def test_media_type_flags_enabled_kinds_all_disabled_returns_empty() -> None:
    flags = MediaTypeFlags(static_posts=False, reels=False, carousels=False, video_posts=False)
    assert flags.enabled_kinds() == []


def test_load_config_accepts_accounts_only_block(tmp_path: Path) -> None:
    """Backward-compat: a config with `accounts:` and no `posts:` loads."""
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
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    assert len(instagram.accounts) == 1
    assert instagram.posts == []


def test_load_config_accepts_posts_only_block(tmp_path: Path) -> None:
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
    posts:
      - url: "https://www.instagram.com/p/DbRSJ_dqSgR/"
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    assert instagram.accounts == []
    assert [entry.url for entry in instagram.posts] == ["https://www.instagram.com/p/DbRSJ_dqSgR/"]


def test_load_config_accepts_both_blocks(tmp_path: Path) -> None:
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
    posts:
      - url: "https://www.instagram.com/p/DbRSJ_dqSgR/"
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    assert len(instagram.accounts) == 1
    assert len(instagram.posts) == 1


def test_load_config_accepts_neither_block(tmp_path: Path) -> None:
    """Empty scan is valid — both work-lists default to empty."""
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
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    assert instagram.accounts == []
    assert instagram.posts == []


def test_load_config_accepts_reel_url_in_posts_block(tmp_path: Path) -> None:
    """`/reel/` URLs ARE valid post URLs — the validator accepts both `/p/` and `/reel/`."""
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
    posts:
      - url: "https://www.instagram.com/reel/DbI2zKqobrF/"
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    assert [entry.url for entry in instagram.posts] == [
        "https://www.instagram.com/reel/DbI2zKqobrF/"
    ]


def test_load_config_rejects_account_url_in_posts_block(tmp_path: Path) -> None:
    """An account URL pasted into `posts:` is a load-time error, not a runtime `not_found`."""
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
    posts:
      - url: "https://instagram.com/some_username/"
""".strip(),
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(yaml_path)

    message = str(exc_info.value)
    assert "PostConfig.url" in message
    assert "'accounts:'" in message
    assert "'posts:'" in message


def test_load_config_rejects_unknown_key_on_posts_entry(tmp_path: Path) -> None:
    """Strict mode: an extra key on a `posts:` entry fails at load time."""
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
    posts:
      - url: "https://www.instagram.com/p/DbRSJ_dqSgR/"
        unknown_field: 42
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_post_config_resolved_cadence_folds_override() -> None:
    instagram = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        posts=[
            PostConfig(url="https://www.instagram.com/p/AAAAAAAAAAA/", cadence=timedelta(hours=24)),
            PostConfig(url="https://www.instagram.com/p/BBBBBBBBBBB/"),
        ],
    )

    override, default = instagram.posts
    assert override.resolved_cadence(instagram) == timedelta(hours=24)
    assert default.resolved_cadence(instagram) == timedelta(hours=6)


def test_enumerate_configured_posts_preserves_config_order() -> None:
    instagram = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
        posts=[
            PostConfig(url="https://www.instagram.com/p/FIRST_______/"),
            PostConfig(url="https://www.instagram.com/reel/SECOND______/"),
            PostConfig(url="https://www.instagram.com/p/THIRD_______/"),
        ],
    )

    assert enumerate_configured_posts(instagram) == [
        "https://www.instagram.com/p/FIRST_______/",
        "https://www.instagram.com/reel/SECOND______/",
        "https://www.instagram.com/p/THIRD_______/",
    ]


def test_enumerate_configured_posts_empty_returns_empty_list() -> None:
    instagram = SourceConfig(
        default_cadence=timedelta(hours=6),
        default_media_types=MediaTypeFlags(),
    )

    assert enumerate_configured_posts(instagram) == []


# ── AccountConfig.backend field ────────────────────────────────────────────


def test_account_config_backend_defaults_to_anonymous() -> None:
    """No `backend:` key in YAML → default `"anonymous"` (backward-compat)."""
    account = AccountConfig(url="https://instagram.com/some_venue/")
    assert account.backend == "anonymous"


def test_account_config_backend_accepts_hikerapi_literal() -> None:
    account = AccountConfig(url="https://instagram.com/some_venue/", backend="hikerapi")
    assert account.backend == "hikerapi"


def test_account_config_backend_rejects_unknown_backend_literal() -> None:
    with pytest.raises(ValidationError):
        AccountConfig(
            url="https://instagram.com/some_venue/",
            backend="playwright",  # type: ignore[arg-type]
        )


def test_load_config_folds_backend_from_yaml(tmp_path: Path) -> None:
    """`backend:` in YAML lands on the model; absent entries default to anonymous."""
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
      - url: "https://instagram.com/creator_a"
      - url: "https://instagram.com/business_venue"
        backend: "hikerapi"
""".strip(),
    )

    instagram = load_config(yaml_path).sources["instagram"]
    default_backend, explicit_backend = instagram.accounts
    assert default_backend.backend == "anonymous"
    assert explicit_backend.backend == "hikerapi"
