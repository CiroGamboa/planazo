"""HW4 Part 3 batch safety pass over stored MLflow traces.

Reads every trace in the configured MLflow experiment, runs the
detector composed in `planazo.safety.detect.detect_safety_issues`
over each, and writes a markdown summary + one JSONL row per flagged
finding. Also, when `--run-attacks` is passed, first drives the
Recommender through `data/eval/attack_scenarios.jsonl` so the
attacks land as fresh traces that the same detector then scores.

`--attacks-only`: skip the batch, run the attacks, score just those
traces. Useful when iterating on detector rules.

Usage:
    uv run python scripts/run_safety_batch.py
    uv run python scripts/run_safety_batch.py --run-attacks
    uv run python scripts/run_safety_batch.py --attacks-only

Layer 2 and Layer 4 defenses are enforced elsewhere in the codebase
and cited (not re-checked) by this runner — see ADR 0027 decision 6.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planazo.observability.tracing import configure_tracing
from planazo.safety import SafetyFinding, detect_safety_issues

DEFAULT_EXPERIMENT = "planazo"
DEFAULT_OUT_DIR = Path("data/eval/results")


class AttackScenario(BaseModel):
    """One row from `data/eval/attack_scenarios.jsonl`.

    Same shape as `AgentEvalCase` so the two files stay swappable — the
    detector doesn't care whether the trace came from an attack or from
    an eval case.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    expected_tools: list[dict[str, Any]] = Field(default_factory=list)
    expected_outcome: str = Field(min_length=1)
    notes: str = ""


def _load_attacks(path: Path) -> list[AttackScenario]:
    """Load attack scenarios from JSONL, one row per line."""
    scenarios: list[AttackScenario] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
                scenarios.append(AttackScenario.model_validate(payload))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"{path}:{line_number}: bad row") from exc
    return scenarios


def _run_attacks(
    scenarios: list[AttackScenario],
    *,
    user_id: int,
    force_trace: bool = False,
) -> list[str]:
    """Drive the Recommender through each attack; return the trace_ids.

    Two modes:

    - Default: run each attack's input through `planazo.query.interpret`
      first. If the interpreter classifies the input as `chat` (small
      talk / meta-question), the recommender never runs and the attack
      leaves no trace for the detector to score — the interpreter is
      acting as a de-facto pre-Layer-1 filter and we report that.
    - `force_trace=True`: bypass the interpreter and hand every attack
      a canned SearchIntent so it always reaches `run_once` and lands
      as a trace. This is the mode HW4 Part 3's report needs — the
      detector must be end-to-end tested against every declared attack,
      not just the ones the interpreter happens to let through.
    """
    from datetime import UTC, datetime, timedelta

    from planazo.agents.event_agent import run_once
    from planazo.query.interpreter import interpret
    from planazo.query.models import SearchIntent

    trace_ids: list[str] = []
    for scenario in scenarios:
        if force_trace:
            # Canned intent that unambiguously reaches run_once. Uses a
            # broad time window over the next 30 days so the recommender
            # cannot reject on the time boundary; empty categories keep
            # the default Recommender surface.
            now = datetime.now(UTC)
            intent = SearchIntent(
                start_utc=now,
                end_utc=now + timedelta(days=30),
                categories=(),
                city="Barcelona",
            )
            reason = "forced (interpreter bypassed)"
        else:
            routed = interpret(scenario.input)
            if routed.kind == "chat":
                print(f"[attack] {scenario.case_id}: router-deflected (no trace)")
                continue
            intent = routed.intent
            reason = "traced"
        try:
            run_once(
                user_id=user_id,
                intent=intent,
                text=scenario.input,
                request_origin="batch",
                eval_case_id=f"attack:{scenario.case_id}",
            )
        except Exception as exc:
            print(f"[attack] {scenario.case_id}: run_once raised {exc!r}")
            continue
        # The last trace in the current experiment is this run's.
        if hasattr(mlflow, "get_last_active_trace_id"):
            last = mlflow.get_last_active_trace_id()
        else:
            last = None
        if last:
            trace_ids.append(last)
        print(f"[attack] {scenario.case_id}: {reason}")
    return trace_ids


def _score_traces(
    experiment_name: str,
    *,
    tag_filter: str | None = None,
    limit: int | None = None,
) -> list[tuple[str, list[SafetyFinding], dict[str, str]]]:
    """Pull traces from the experiment and run the detector over each."""
    experiment = mlflow.get_experiment_by_name(experiment_name)
    if experiment is None:
        return []
    traces = mlflow.search_traces(
        experiment_ids=[experiment.experiment_id],
        max_results=limit,
        return_type="list",
    )
    results: list[tuple[str, list[SafetyFinding], dict[str, str]]] = []
    for trace in traces:
        tags = {k: v for k, v in trace.info.tags.items() if not k.startswith("mlflow.")}
        if tag_filter and not any(tag_filter in v for v in tags.values()):
            continue
        findings = detect_safety_issues(trace)
        results.append((trace.info.request_id, findings, tags))
    return results


def _write_markdown(
    results: list[tuple[str, list[SafetyFinding], dict[str, str]]],
    out_path: Path,
) -> None:
    """Write a summary table + false-positive rollup to markdown."""
    total = len(results)
    flagged = sum(1 for _, findings, _ in results if findings)
    legit = [r for r in results if not r[2].get("eval_case_id", "").startswith("attack:")]
    attacks = [r for r in results if r[2].get("eval_case_id", "").startswith("attack:")]
    fp_count = sum(1 for _, findings, _ in legit if findings)

    lines: list[str] = []
    lines.append(f"# HW4 Safety Batch — {datetime.now(UTC).isoformat(timespec='seconds')}\n")
    lines.append(f"- Traces scored: **{total}**")
    lines.append(f"- Findings on any trace: **{flagged}**")
    lines.append(f"- Attack traces: **{len(attacks)}**")
    lines.append(f"- Legitimate traces: **{len(legit)}**")
    lines.append(f"- False positives (legitimate scored as flagged): **{fp_count}**\n")

    lines.append("## Findings\n")
    lines.append("| trace_id | case_id | request_origin | layer | kind | evidence |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for trace_id, findings, tags in results:
        if not findings:
            continue
        case = tags.get("eval_case_id", "-")
        origin = tags.get("request_origin", "-")
        for finding in findings:
            lines.append(
                f"| `{trace_id[:12]}` | {case} | {origin} | "
                f"{finding.layer} | {finding.kind} | `{finding.evidence}` |"
            )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_jsonl(
    results: list[tuple[str, list[SafetyFinding], dict[str, str]]],
    out_path: Path,
) -> None:
    """One JSONL row per (trace, finding)."""
    with out_path.open("w", encoding="utf-8") as fh:
        for trace_id, findings, tags in results:
            for finding in findings:
                row = {
                    "trace_id": trace_id,
                    "case_id": tags.get("eval_case_id"),
                    "request_origin": tags.get("request_origin"),
                    **finding.model_dump(),
                }
                fh.write(json.dumps(row) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "safety batch")
    parser.add_argument(
        "--experiment",
        default=DEFAULT_EXPERIMENT,
        help="MLflow experiment to scan (default: planazo)",
    )
    parser.add_argument(
        "--attacks",
        default=Path("data/eval/attack_scenarios.jsonl"),
        type=Path,
        help="attack scenarios JSONL",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        type=Path,
        help="output directory for the markdown + JSONL summaries",
    )
    parser.add_argument(
        "--run-attacks",
        action="store_true",
        help="drive the recommender through the attack scenarios first",
    )
    parser.add_argument(
        "--attacks-only",
        action="store_true",
        help="score only the attack traces (implies --run-attacks)",
    )
    parser.add_argument(
        "--force-trace",
        action="store_true",
        help=(
            "bypass the query interpreter and hand each attack a canned "
            "SearchIntent, so every attack lands as a trace the detector "
            "can score (default: let the interpreter deflect)"
        ),
    )
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    configure_tracing(experiment=args.experiment)

    if args.run_attacks or args.attacks_only:
        scenarios = _load_attacks(args.attacks)
        print(f"Running {len(scenarios)} attack scenarios…")
        _run_attacks(scenarios, user_id=args.user_id, force_trace=args.force_trace)

    tag_filter = "attack:" if args.attacks_only else None
    results = _score_traces(args.experiment, tag_filter=tag_filter, limit=args.limit)

    args.out.mkdir(parents=True, exist_ok=True)
    md_path = args.out / "safety_batch.md"
    jsonl_path = args.out / "safety_findings.jsonl"
    _write_markdown(results, md_path)
    _write_jsonl(results, jsonl_path)

    total = len(results)
    with_findings = sum(1 for _, findings, _ in results if findings)
    print(f"\nScored {total} traces; {with_findings} carry safety findings.")
    print(f"Wrote {md_path}")
    print(f"Wrote {jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
