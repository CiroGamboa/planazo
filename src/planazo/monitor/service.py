"""Run-log loading, joining, grading, and report generation for the monitor CLI."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from planazo.monitor.judge import grade_run
from planazo.monitor.models import GradedRun, RunSession, RunStep, Verdict

Judge = Callable[[RunSession], Verdict]


class MonitorDataError(ValueError):
    """A persisted trace was not a valid monitor boundary artifact."""


def repository_root() -> Path:
    """Find the repository root from this installed source tree.

    `service.py` lives at `src/planazo/monitor/service.py`; walking three
    parents up lands on the repo root (ADR 0009 — the outer `agent/`
    directory was retired in favor of a flat repo-root layout).
    """
    return Path(__file__).resolve().parents[3]


def parse_since(value: str) -> timedelta:
    """Parse a compact duration such as ``24h`` or ``7d``."""
    units = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60, "w": 7 * 24 * 60 * 60}
    if len(value) < 2 or value[-1] not in units or not value[:-1].isdigit():
        raise ValueError("--since must be a positive duration such as 24h, 7d, or 1w")
    amount = int(value[:-1])
    if amount < 1:
        raise ValueError("--since must be positive")
    return timedelta(seconds=amount * units[value[-1]])


def _read_trace_file(path: Path) -> list[RunStep]:
    entries: list[RunStep] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(RunStep.model_validate_json(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise MonitorDataError(f"invalid monitor trace at {path}:{line_number}: {exc}") from exc
    return entries


def read_runs(paths: Iterable[Path]) -> list[RunStep]:
    """Read and validate every JSONL trace line from the requested files."""
    return [entry for path in paths if path.exists() for entry in _read_trace_file(path)]


def join_runs(entries: Iterable[RunStep]) -> list[RunSession]:
    """Group Recommender and Extractor trace lines into stable run sessions."""
    grouped: dict[str, list[RunStep]] = defaultdict(list)
    for entry in entries:
        grouped[entry.run_id].append(entry)
    return [
        RunSession(
            run_id=run_id,
            started_at=min(step.started_at for step in steps),
            steps=sorted(steps, key=lambda step: (step.recorded_at, step.agent, step.step)),
        )
        for run_id, steps in sorted(grouped.items())
    ]


def select_runs(
    runs: Iterable[RunSession], *, since: timedelta, now: datetime | None = None
) -> list[RunSession]:
    """Keep runs whose start timestamp falls in the requested rolling window."""
    reference = now or datetime.now(UTC)
    cutoff = reference - since
    return [run for run in runs if run.started_at >= cutoff]


def _report_text(day: date, graded_runs: list[GradedRun]) -> str:
    lines = [
        f"# Planazo monitor report — {day.isoformat()}",
        "",
        f"Runs graded: {len(graded_runs)}",
        "",
        "## Verdicts",
        "",
    ]
    for graded in graded_runs:
        verdict = graded.verdict
        lines.extend(
            [
                f"### {graded.run_id}",
                "",
                f"- Prompt adherence: `{verdict.prompt_adherence}`",
                f"- Untrusted-content handling: `{verdict.untrusted_content_handling}`",
            ]
        )
        if verdict.rationale is not None:
            lines.extend(
                [
                    f"- Expected: {verdict.rationale.expected}",
                    f"- Actual: {verdict.rationale.actual}",
                ]
            )
        lines.append("")

    findings = [
        graded
        for graded in graded_runs
        if graded.verdict.prompt_adherence != "strictly_adheres"
        or graded.verdict.untrusted_content_handling != "safe"
    ]
    lines.extend(["## Real problems found", ""])
    if not findings:
        lines.append("None.")
    else:
        for graded in findings:
            rationale = graded.verdict.rationale
            assert rationale is not None
            lines.append(
                f"- `{graded.run_id}`: expected {rationale.expected}; observed {rationale.actual}"
            )
    lines.append("")
    return "\n".join(lines)


def write_reports(graded_runs: Iterable[GradedRun], output_dir: Path) -> list[Path]:
    """Write one Markdown report and JSONL sidecar for every run-start day."""
    grouped: dict[date, list[GradedRun]] = defaultdict(list)
    for graded in graded_runs:
        grouped[graded.started_at.date()].append(graded)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []
    for day, day_runs in sorted(grouped.items()):
        day_runs.sort(key=lambda item: (item.started_at, item.run_id))
        stem = output_dir / day.isoformat()
        markdown_path = stem.with_suffix(".md")
        jsonl_path = stem.with_suffix(".jsonl")
        markdown_path.write_text(_report_text(day, day_runs), encoding="utf-8")
        jsonl_path.write_text(
            "".join(
                json.dumps(graded.model_dump(mode="json"), separators=(",", ":")) + "\n"
                for graded in day_runs
            ),
            encoding="utf-8",
        )
        output_paths.extend([markdown_path, jsonl_path])
    return output_paths


def run_monitor(
    *,
    recommender_dir: Path,
    extractor_log: Path,
    output_dir: Path,
    since: timedelta,
    judge: Judge = grade_run,
    now: datetime | None = None,
    run_ids: set[str] | None = None,
) -> list[Path]:
    """Load, join, grade, and report every run in the requested time window."""
    recommender_paths = sorted(recommender_dir.glob("*.jsonl"))
    entries = read_runs([*recommender_paths, extractor_log])
    runs = select_runs(join_runs(entries), since=since, now=now)
    if run_ids is not None:
        runs = [run for run in runs if run.run_id in run_ids]
    graded = [
        GradedRun(run_id=run.run_id, started_at=run.started_at, verdict=judge(run)) for run in runs
    ]
    return write_reports(graded, output_dir)
