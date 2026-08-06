"""The ``planazo-agent-eval`` console entrypoint.

Runs the HW4 Part 1 agent-eval harness — reads the committed scenario
file, runs each scenario ``--runs`` times against the real LLM at a
sweeping ``--temperature``, scores tool-selection + trajectory
precision/recall per run, and writes:

- ``{out}/agent_eval_per_case.jsonl`` — one JSONL row per (case,
  run_index) with the run's status, answer, tool trajectory, per-metric
  scores, latency, and MLflow trace id.
- ``{out}/agent_eval.md`` — a Markdown roll-up with ``pass@3`` /
  ``pass^3`` per scenario plus per-metric averages.

The roll-up table is also printed to stdout so a shell run is legible.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from agentlib.core import CHEAP
from planazo.eval.agent.models import RunResult, ScenarioResult
from planazo.eval.agent.runner import run_scenario
from planazo.eval.agent.scenarios import load_agent_scenarios
from planazo.observability.tracing import configure_tracing

_DEFAULT_SCENARIOS = Path("data/eval/agent_scenarios.jsonl")
_DEFAULT_OUT = Path("data/eval/results")
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_RUNS = 3
_DEFAULT_USER_ID = 1
_PASS_THRESHOLD = 0.5


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="planazo-agent-eval",
        description=__doc__,
    )
    parser.add_argument("--scenarios", type=Path, default=_DEFAULT_SCENARIOS)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--temperature", type=float, default=_DEFAULT_TEMPERATURE)
    parser.add_argument("--runs", type=int, default=_DEFAULT_RUNS)
    parser.add_argument("--user-id", type=int, default=_DEFAULT_USER_ID)
    parser.add_argument("--model", type=str, default=CHEAP)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of scenarios run (default: all)",
    )
    return parser.parse_args(argv)


def _append_run(path: Path, run: RunResult) -> None:
    """Atomically append one JSON-serialised ``RunResult`` to ``path``.

    Mirrors ``planazo.scheduler.audit.append_run_record``: one JSON object
    per line, no buffering across records, parent directory created on
    first write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(run.model_dump(mode="json"), separators=(",", ":")))
        handle.write("\n")


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def _scenario_averages(scenario: ScenarioResult) -> tuple[float | None, float | None, float | None]:
    """Return per-scenario means of (tool_selection, traj_precision, traj_recall).

    ``None`` entries in the run rows (a harness abort with no scored
    trace) are excluded from the mean so the reported average always
    reflects real observed runs.
    """

    def _collect(attr: str) -> list[float]:
        return [
            score
            for run in scenario.runs
            if (score := getattr(run, attr)) is not None
        ]

    return (
        _mean(_collect("tool_selection")),
        _mean(_collect("traj_precision")),
        _mean(_collect("traj_recall")),
    )


def _render_markdown(scenarios: list[ScenarioResult], *, temperature: float, runs: int) -> str:
    """Build the ``agent_eval.md`` roll-up document."""
    header = (
        "# Agent eval — HW4 Part 1 (Recommender)\n\n"
        f"- Scenarios evaluated: {len(scenarios)}\n"
        f"- Runs per scenario: {runs}\n"
        f"- Temperature: {temperature}\n"
        f"- pass threshold (tool_selection): {_PASS_THRESHOLD}\n\n"
        "| case_id | pass@3 | pass^3 | avg_tool_selection | "
        "avg_traj_precision | avg_traj_recall |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    rows: list[str] = []
    for scenario in scenarios:
        avg_ts, avg_tp, avg_tr = _scenario_averages(scenario)
        pass_at_3 = "yes" if scenario.pass_at_3(_PASS_THRESHOLD) else "no"
        pass_cubed = "yes" if scenario.pass_cubed(_PASS_THRESHOLD) else "no"
        rows.append(
            f"| {scenario.case_id} | {pass_at_3} | {pass_cubed} | "
            f"{_fmt(avg_ts)} | {_fmt(avg_tp)} | {_fmt(avg_tr)} |"
        )
    return header + "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    configure_tracing()

    cases = load_agent_scenarios(args.scenarios)
    if args.limit is not None:
        cases = cases[: args.limit]

    args.out.mkdir(parents=True, exist_ok=True)
    per_case_path = args.out / "agent_eval_per_case.jsonl"
    markdown_path = args.out / "agent_eval.md"

    # Truncate the per-case JSONL so a rerun does not accumulate old rows.
    per_case_path.write_text("", encoding="utf-8")

    scenarios: list[ScenarioResult] = []
    for case in cases:
        print(
            f"[{case.case_id}] running {args.runs} run(s) at temperature={args.temperature}",
            file=sys.stderr,
        )
        scenario = run_scenario(
            case,
            temperature=args.temperature,
            run_count=args.runs,
            user_id=args.user_id,
            model=args.model,
        )
        scenarios.append(scenario)
        for run in scenario.runs:
            _append_run(per_case_path, run)

    document = _render_markdown(scenarios, temperature=args.temperature, runs=args.runs)
    markdown_path.write_text(document, encoding="utf-8")

    print(document)
    print(
        f"\nWrote {markdown_path} and {per_case_path}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
