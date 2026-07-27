"""`planazo-sources-instagram` — run the Instagram adapter from a container.

The CLI loads `data/sources.yaml`, resolves the per-account fetch plan (one
line per `(account, media_type)` pair that the account has enabled), and
either prints the plan (`--dry-run`) or exercises `InstagramSource.fetch_post`
against each configured account URL and prints the JSON payload — either
`RawPost.model_dump_json()` or the typed error dict — one line per fetch.

The CLI is the entrypoint the Docker service invokes; `docker compose up
sources-instagram` runs `planazo-sources-instagram` inside the container.
Session loading is opt-in via the `INSTAGRAM_SESSION_ID` env var; absent it
the client stays anonymous (ADR 0006, decision 5).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from planazo.sources.config import (
    AccountConfig,
    MediaTypeFlags,
    SourceConfig,
    load_config,
)
from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.instagram.client import InstagramClient
from planazo.sources.models import RawPost

_MEDIA_TYPE_FIELDS: tuple[str, ...] = ("static_posts", "reels", "carousels", "video_posts")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Planazo Instagram source adapter.")
    parser.add_argument(
        "--config",
        default="data/sources.yaml",
        help="path to the sources config (default: data/sources.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved fetch plan without any network calls",
    )
    return parser


def _enabled_media_types(flags: MediaTypeFlags) -> list[str]:
    """Return the enabled media-type field names in declaration order."""
    return [name for name in _MEDIA_TYPE_FIELDS if getattr(flags, name)]


def _plan_lines(source: SourceConfig) -> list[str]:
    """Build the (account, media-type) plan the CLI prints in `--dry-run`."""
    lines: list[str] = []
    for account in source.accounts:
        media_types = account.resolved_media_types(source)
        cadence = account.resolved_cadence(source)
        for media_type in _enabled_media_types(media_types):
            lines.append(f"{account.url} {media_type} cadence={cadence}")
    return lines


def _emit_fetch(account: AccountConfig, adapter: InstagramSource) -> str:
    """Fetch one account URL and return the JSON line to print."""
    result = adapter.fetch_post(account.url)
    if isinstance(result, RawPost):
        return result.model_dump_json()
    return json.dumps(result)


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for `planazo-sources-instagram`."""
    args = _parser().parse_args(argv)
    config = load_config(Path(args.config))
    instagram = config.sources.get("instagram")
    if instagram is None:
        print("no 'instagram' source in config", file=sys.stderr)
        return 1

    if args.dry_run:
        for line in _plan_lines(instagram):
            print(line)
        return 0

    client = InstagramClient()
    client.load_session_from_env()
    adapter = InstagramSource(instagram, client)
    for account in instagram.accounts:
        print(_emit_fetch(account, adapter))
    return 0


if __name__ == "__main__":
    sys.exit(main())
