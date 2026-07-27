"""Command-line entrypoint for the independent Planazo run monitor."""

from __future__ import annotations

import argparse
from pathlib import Path

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
    return parser


def main() -> None:
    """Run the monitor over live logs or deterministic seed logs."""
    args = _parser().parse_args()
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

    written = run_monitor(
        recommender_dir=recommender_dir,
        extractor_log=extractor_log,
        output_dir=output_dir,
        since=since,
    )
    print(f"graded reports written: {', '.join(str(path) for path in written) or 'none'}")
