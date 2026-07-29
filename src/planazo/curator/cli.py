"""`planazo-curator` — cron entry point for the catalog curator agent.

One subcommand, `--tick`, drives one curator pass end-to-end. See
[ADR 0020](../../../docs/adr/0020-catalog-curator-agent.md) for the
charter.

Modifiers:

- `--dry-run` — the LLM sees the same tool interfaces but the write
  tools return `{"status": "dry_run", ...}` instead of mutating the DB.
  `agent_runs` still records the tick (so the run is auditable), but
  `llm_decisions` does not fire for the dry-run write tools — nothing
  was actually decided into DB state. The JSONL audit log marks the
  record with `dry_run: true`.
- `--verbose` — a stdout narrative stream describing each tool call
  the LLM made. Off by default (cron runs stay one-line-per-tick).
  Mirrors the shape of `planazo-scheduler --verbose`. See ADR 0017 for
  the Rule-2 discipline: no captions or event descriptions ever cross
  into the narrative; only ids, counts, and Literal-valued fields.

Exit-code taxonomy (mirrors `planazo-scheduler`):

- **`0`** — tick completed. Any completed tick returns `0` regardless of
  the LLM's per-decision outcomes. Operators read `var/curator_runs.jsonl`
  and `data/monitor/*.md` for per-decision health.
- **`1`** — uncaught exception. Any exception that escapes `run_curator`
  — Pydantic validation on the record boundary, DB corruption,
  filesystem unwritable, LLM driver blow-up. One-liner to stderr with
  the exception class + a 120-char-truncated message (Rule 2 discipline
  extends to the exception-hoist path).
- **`2`** — configuration-time failure. Reserved; the curator has no
  boot-time YAML today, but the code path is here for future
  `data/curator.yaml`-style config additions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from planazo.agents.loop import LoopResult, StepRecord
from planazo.curator.models import DEFAULT_AUDIT_LOG_PATH
from planazo.curator.service import run_curator

__all__ = ["main"]

EXIT_OK: Final[int] = 0
EXIT_UNCAUGHT: Final[int] = 1
EXIT_CONFIG: Final[int] = 2

_EXCEPTION_MESSAGE_TRUNCATE: Final[int] = 120
"""Max length of `str(exc)` on the exit-`1` stderr line — Rule 2 discipline."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planazo-curator",
        description=(
            "Daily curator tick: archive stale events, merge duplicates, "
            "correct mis-classified categories. See ADR 0020."
        ),
    )
    parser.add_argument(
        "--tick",
        action="store_true",
        required=True,
        help="Run one curator pass. Exit 0 on completion, 1 on uncaught exception.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        default=False,
        help="LLM sees the tools but write tools return dry_run payloads "
        "instead of mutating the DB. agent_runs still records the tick.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print a step-by-step narrative log to stdout during the tick.",
    )
    return parser


def _print_narrative_step(record: StepRecord) -> None:
    """Emit one narrative line per tool call — Rule 2: structural fields only.

    IDs, counts, and Literal-valued fields are safe. Never any Event
    text (title, description, venue_name) — those flow through
    `llm_decisions.rationale` DB-inside where the trust boundary allows
    full LLM reasoning.
    """
    tool = record.tool
    arguments = record.arguments if isinstance(record.arguments, dict) else {}
    result = record.result if isinstance(record.result, dict) else {}

    if tool in {"list_stale_events", "list_duplicate_candidates", "list_low_confidence_events"}:
        total = result.get("total") if isinstance(result, dict) else None
        print(f"[{record.step:02d}] {tool}(...) -> {total} row(s)")
        return

    status = result.get("status", "?")
    if tool == "archive_event":
        event_id = arguments.get("event_id")
        print(f"[{record.step:02d}] archive_event(event_id={event_id}) -> {status}")
    elif tool == "merge_events":
        keep_id = arguments.get("keep_event_id")
        archive_ids = arguments.get("archive_event_ids", [])
        count = len(archive_ids) if isinstance(archive_ids, list) else 0
        print(
            f"[{record.step:02d}] merge_events(keep={keep_id}, archive={count} id(s)) -> {status}"
        )
    elif tool == "update_event_category":
        event_id = arguments.get("event_id")
        new_cat = arguments.get("new_category")
        print(
            f"[{record.step:02d}] update_event_category(event_id={event_id},"
            f" new_category={new_cat!r}) -> {status}"
        )
    else:
        print(f"[{record.step:02d}] {tool}(...) -> {status}")


def _print_narrative_complete(result: LoopResult) -> None:
    print(f"[--] loop terminal: stopped={result.stopped}, steps={result.steps}")


def _print_uncaught_error(exc: BaseException) -> None:
    detail = str(exc).replace("\n", " ").replace("\t", " ")
    truncated = detail[:_EXCEPTION_MESSAGE_TRUNCATE]
    print(
        f"planazo-curator: uncaught {type(exc).__name__}: {truncated}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point wired via `pyproject.toml [project.scripts]`.

    `argv=None` reads `sys.argv[1:]`; tests inject a list.
    """
    args = _build_parser().parse_args(argv)

    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH
    verbose = bool(args.verbose)
    on_step = _print_narrative_step if verbose else None
    on_complete = _print_narrative_complete if verbose else None

    try:
        tick_result = run_curator(
            dry_run=bool(args.dry_run),
            audit_log_path=audit_log_path,
            on_step=on_step,
            on_complete=on_complete,
        )
    except Exception as exc:
        _print_uncaught_error(exc)
        return EXIT_UNCAUGHT

    print(
        f"tick: run_id={tick_result.run_id[:8]} stopped={tick_result.stopped}"
        f" steps={tick_result.steps}"
        f" archived={tick_result.events_archived}"
        f" merged={tick_result.events_merged}"
        f" updated={tick_result.categories_updated}"
        f" errors={len(tick_result.errors)}"
        f" dry_run={tick_result.dry_run}"
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
