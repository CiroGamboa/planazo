"""Command-line entrypoint for the independent Planazo run monitor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openai

from planazo.config import check_api_key
from planazo.monitor.service import parse_since, repository_root, run_monitor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grade Planazo agent runs out of band.")
    parser.add_argument("--since", default="24h", help="rolling window, for example 24h or 7d")
    parser.add_argument(
        "--out", default="data/monitor", help="repository-relative output directory"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="grade the deterministic monitor seed runs"
    )
    parser.add_argument(
        "--run-id",
        action="append",
        dest="run_ids",
        help="grade only this run ID (repeatable; useful with --dry-run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the monitor over live logs or deterministic seed logs."""
    args = _parser().parse_args(argv)
    if not check_api_key():
        return 1

    root = repository_root()
    output_dir = Path(args.out)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    if args.dry_run:
        seed_dir = root / "agent" / "scripts" / "monitor" / "seed_runs"
        recommender_dir = seed_dir / "runs"
        extractor_log = seed_dir / "extraction_runs.jsonl"
        since = parse_since("9999w")
    else:
        recommender_dir = root / "data" / "runs"
        extractor_log = root / "agent" / "var" / "extraction_runs.jsonl"
        since = parse_since(args.since)

    try:
        written = run_monitor(
            recommender_dir=recommender_dir,
            extractor_log=extractor_log,
            output_dir=output_dir,
            since=since,
            run_ids=set(args.run_ids) if args.run_ids else None,
        )
    except openai.OpenAIError as exc:
        print(str(exc))
        return 1
    print(f"graded reports written: {', '.join(str(path) for path in written) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
