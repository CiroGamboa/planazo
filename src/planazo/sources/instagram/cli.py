"""`planazo-sources-instagram` — run the Instagram adapter from a container.

The CLI loads `data/sources.yaml` and runs in one of two modes:

- `--dry-run` — resolve the per-account fetch plan and print one line per
  `(account, media_type)` pair the account has enabled, without any network
  calls.
- `--url <post_url>` — fetch one specific post via `InstagramSource.fetch_post`
  and print the JSON payload — either `RawPost.model_dump_json()` or the
  typed error dict — on stdout, one line, then exit 0.

Exactly one of the two mode flags is required; the two are mutually exclusive.
Neither given → the CLI exits with a usage error (exit code 2).

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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved fetch plan without any network calls",
    )
    mode.add_argument(
        "--url",
        default=None,
        help="fetch one Instagram post by URL and print the JSON payload",
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


def _emit_fetch(url: str, adapter: InstagramSource) -> str:
    """Fetch one URL and return the JSON line to print."""
    result = adapter.fetch_post(url)
    if isinstance(result, RawPost):
        return result.model_dump_json()
    return json.dumps(result)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: type[InstagramClient] = InstagramClient,
) -> int:
    """Entrypoint for `planazo-sources-instagram`.

    `client_factory` lets tests inject a fake `InstagramClient` subclass with
    the same shape; production callers accept the default.
    """
    args = _parser().parse_args(argv)
    if not args.dry_run and args.url is None:
        print(
            "planazo-sources-instagram: one of --dry-run or --url is required",
            file=sys.stderr,
        )
        return 2

    config = load_config(Path(args.config))
    instagram = config.sources.get("instagram")
    if instagram is None:
        print("no 'instagram' source in config", file=sys.stderr)
        return 1

    if args.dry_run:
        for line in _plan_lines(instagram):
            print(line)
        return 0

    client = client_factory()
    client.load_session_from_env()
    adapter = InstagramSource(instagram, client)
    print(_emit_fetch(args.url, adapter))
    return 0


if __name__ == "__main__":
    sys.exit(main())
