"""HW4 Part 2 scorer batch: run HW3 retrieval + generation + Part 1 metrics
over stored MLflow traces and write results back via `mlflow.log_feedback`.

The scorer bodies (`planazo.eval.metrics.retrieval`,
`planazo.eval.metrics.generation`, `planazo.eval.agent.metrics`) are NOT
modified. All wiring lives in this file plus the three adapters at
`planazo.eval.agent.adapters`. That is the Session-12 invariant: if
wiring a scorer would need to edit it, the adapter is doing too little.

Usage:
    uv run python scripts/run_trace_scorers.py
    uv run python scripts/run_trace_scorers.py --experiment planazo
    uv run python scripts/run_trace_scorers.py --limit 5

Golden lookup: retrieval scorers need a golden id list per scenario. This
runner reads `data/eval/agent_scenarios.jsonl` and matches by the trace's
`eval_case_id` tag — scenarios without a golden list contribute no
retrieval-scorer output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

from planazo.eval.agent.adapters import (
    trace_to_generation_inputs,
    trace_to_retrieval_inputs,
    trace_to_tool_calls,
)
from planazo.eval.agent.metrics import (
    goal_completion_score,
    tool_selection_accuracy,
    trajectory_precision_recall,
)
from planazo.eval.agent.scenarios import load_agent_scenarios
from planazo.eval.dataset import load_golden_cases
from planazo.eval.judge import OpenCodeJudge
from planazo.eval.metrics.generation import (
    score_answer_relevance,
    score_context_precision,
    score_faithfulness,
)
from planazo.eval.metrics.retrieval import (
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from planazo.observability.tracing import configure_tracing

DEFAULT_EXPERIMENT = "planazo"
DEFAULT_K = 5


def _extract_answer(trace: object) -> str | None:
    """Best-effort read of the root AGENT span's answer output."""
    spans = getattr(getattr(trace, "data", None), "spans", None) or []
    for span in spans:
        if getattr(span, "span_type", None) != "AGENT":
            continue
        attrs = getattr(span, "attributes", {})
        if not hasattr(attrs, "get"):
            continue
        outputs = attrs.get("mlflow.spanOutputs")
        if isinstance(outputs, dict):
            ans = outputs.get("answer")
            if isinstance(ans, str) and ans:
                return ans
    return None


def _resolve_golden_for_case(
    agent_scenarios_path: Path,
    questions_path: Path,
    case_id: str,
) -> list[str] | None:
    """Best-effort join from an agent scenario's case_id to a golden id list.

    The agent scenarios file does not carry golden ids directly (they are
    outcome descriptions, not retrieval-golden). This helper falls back to
    the HW3 questions file: if a scenario shares the same case_id with a
    golden case there, use that case's `golden_event_ids`.

    Returns `None` when no golden ids are known — the scorer batch skips
    retrieval scoring for that trace rather than send an empty golden.
    """
    if not questions_path.exists():
        return None
    goldens = {case.id: case.golden_event_ids for case in load_golden_cases(questions_path)}
    return goldens.get(case_id)


_JUDGE_SCORER_NAMES = frozenset({
    "goal_completion",
    "faithfulness",
    "answer_relevance",
    "context_precision",
})

_client: MlflowClient | None = None


def _get_client() -> MlflowClient:
    """Lazy-instantiate the client so it captures the tracking URI set by
    `configure_tracing()` at CLI startup, not the module-import default."""
    global _client
    if _client is None:
        _client = MlflowClient()
    return _client


def _log_feedback_safe(
    *,
    name: str,
    value: float,
    trace_id: str,
    rationale: str | None = None,
) -> None:
    """Attach one scorer output to a trace.

    `mlflow.log_feedback(...)` is currently Databricks-managed-MLflow only
    (open-source ships the API but the file backend raises "not
    supported"). We fall back to `MlflowClient.set_trace_tag()` under the
    `feedback.<metric>` prefix — the trace still carries the scorer
    result, MLflow UI shows it in the tag column, and downstream tools
    can filter on it. Same story either way: "the scorer wrote its
    result back to the trace". See ADR 0027 decision 4.
    """
    try:
        _get_client().set_trace_tag(trace_id, f"feedback.{name}", f"{value:.4f}")
        source_label = "llm_judge" if name in _JUDGE_SCORER_NAMES else "code"
        _get_client().set_trace_tag(trace_id, f"feedback.{name}.source", source_label)
        if rationale:
            trimmed = rationale if len(rationale) < 500 else rationale[:499] + "…"
            _get_client().set_trace_tag(trace_id, f"feedback.{name}.rationale", trimmed)
    except Exception as exc:
        print(f"    set_trace_tag failed for {name} on {trace_id[:12]}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "trace scorers")
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--scenarios",
        default=Path("data/eval/agent_scenarios.jsonl"),
        type=Path,
    )
    parser.add_argument(
        "--questions",
        default=Path("data/eval/questions.jsonl"),
        type=Path,
    )
    parser.add_argument(
        "--skip-generation",
        action="store_true",
        help="skip LLM-as-judge generation scorers (avoids API cost during iteration)",
    )
    parser.add_argument(
        "--out",
        default=Path("data/eval/results"),
        type=Path,
        help="output directory for the markdown roll-up (default: data/eval/results/)",
    )
    args = parser.parse_args(argv)

    configure_tracing(experiment=args.experiment)

    experiment = mlflow.get_experiment_by_name(args.experiment)
    if experiment is None:
        print(f"experiment {args.experiment!r} not found")
        return 1

    traces = mlflow.search_traces(
        experiment_ids=[experiment.experiment_id],
        max_results=args.limit,
        return_type="list",
    )
    print(f"Scoring {len(traces)} traces from experiment {args.experiment!r}…")

    scenarios = {case.case_id: case for case in load_agent_scenarios(args.scenarios)}

    judge = None
    if not args.skip_generation:
        try:
            judge = OpenCodeJudge(cache_root=Path("var/eval/judge_cache"))
            print(f"Judge ready (cache at {judge._cache_root}).")
        except Exception as exc:
            print(f"OpenCodeJudge unavailable, skipping generation scorers: {exc}")

    part1_scored = 0
    retrieval_scored = 0
    generation_scored = 0
    goal_scored = 0
    rollup: list[dict[str, object]] = []

    for trace in traces:
        trace_id = trace.info.request_id
        tags = {k: v for k, v in trace.info.tags.items() if not k.startswith("mlflow.")}
        raw_case_id = tags.get("eval_case_id")
        # Strip the -run-N suffix to look up the base scenario.
        base_case_id = raw_case_id.rsplit("-run-", 1)[0] if raw_case_id else None
        print(f"— trace {trace_id[:12]} (case={base_case_id}, origin={tags.get('request_origin')})")

        rollup_row: dict[str, object] = {
            "trace_id": trace_id,
            "case_id": base_case_id,
            "request_origin": tags.get("request_origin"),
        }

        # Part 1 metrics — only meaningful when the trace is an eval trace
        # with a matching scenario.
        if base_case_id and base_case_id in scenarios:
            scenario = scenarios[base_case_id]
            tool_calls = trace_to_tool_calls(trace)
            ts = tool_selection_accuracy(tool_calls, scenario.expected_tools)
            tp, tr = trajectory_precision_recall(tool_calls, scenario.expected_tools)
            _log_feedback_safe(name="tool_selection_accuracy", value=ts, trace_id=trace_id)
            _log_feedback_safe(name="trajectory_precision", value=tp, trace_id=trace_id)
            _log_feedback_safe(name="trajectory_recall", value=tr, trace_id=trace_id)
            part1_scored += 1
            rollup_row.update(
                {
                    "tool_selection": ts,
                    "trajectory_precision": tp,
                    "trajectory_recall": tr,
                }
            )
            print(f"    part1: selection={ts:.2f} precision={tp:.2f} recall={tr:.2f}")

        # Retrieval scorers — need a golden id list
        golden_ids = None
        if base_case_id:
            golden_ids = _resolve_golden_for_case(args.scenarios, args.questions, base_case_id)
        retrieved_ids = trace_to_retrieval_inputs(trace)
        if retrieved_ids is not None and golden_ids:
            scorers = {
                f"hit_at_{DEFAULT_K}": hit_at_k(retrieved_ids, golden_ids, DEFAULT_K),
                f"precision_at_{DEFAULT_K}": precision_at_k(retrieved_ids, golden_ids, DEFAULT_K),
                f"recall_at_{DEFAULT_K}": recall_at_k(retrieved_ids, golden_ids, DEFAULT_K),
                "mrr": mrr(retrieved_ids, golden_ids),
                f"ndcg_at_{DEFAULT_K}": ndcg_at_k(retrieved_ids, golden_ids, DEFAULT_K),
            }
            for name, value in scorers.items():
                if value is not None:
                    _log_feedback_safe(name=name, value=value, trace_id=trace_id)
                    rollup_row[name] = value
            retrieval_scored += 1
            print(f"    retrieval: {scorers}")

        # Generation scorers — need query + answer + chunks + a judge
        if judge is not None:
            gen_inputs = trace_to_generation_inputs(trace)
            if gen_inputs is not None:
                query, answer, chunks = gen_inputs
                try:
                    faith = score_faithfulness(
                        answer=answer, chunks=chunks, judge=judge, case_id=base_case_id or "trace"
                    )
                    rel = score_answer_relevance(
                        query=query,
                        answer=answer,
                        judge=judge,
                        case_id=base_case_id or "trace",
                    )
                    ctx = score_context_precision(
                        query=query,
                        chunks=chunks,
                        judge=judge,
                        case_id=base_case_id or "trace",
                    )
                    _log_feedback_safe(
                        name="faithfulness",
                        value=faith.score,
                        trace_id=trace_id,
                        rationale=faith.rationale,
                    )
                    _log_feedback_safe(
                        name="answer_relevance",
                        value=rel.score,
                        trace_id=trace_id,
                        rationale=rel.rationale,
                    )
                    _log_feedback_safe(
                        name="context_precision",
                        value=ctx.score,
                        trace_id=trace_id,
                        rationale=ctx.rationale,
                    )
                    generation_scored += 1
                    rollup_row.update(
                        {
                            "faithfulness": faith.score,
                            "answer_relevance": rel.score,
                            "context_precision": ctx.score,
                        }
                    )
                    print(
                        f"    generation: F={faith.score:.2f} R={rel.score:.2f} "
                        f"CP={ctx.score:.2f}"
                    )
                except Exception as exc:
                    print(f"    generation scoring failed: {exc}")

        # Goal-completion — Part 1's LLM-as-judge metric. Only needs the
        # trace's final answer, so we extract it directly from the root
        # AGENT span rather than reusing the generation adapter (which
        # also requires a retrieval span). This lets us score
        # goal_completion on preflight-abort traces where no search ran.
        if judge is not None and base_case_id and base_case_id in scenarios:
            root_output_answer = _extract_answer(trace)
            if root_output_answer:
                try:
                    goal = goal_completion_score(
                        question=scenarios[base_case_id].input,
                        expected_outcome=scenarios[base_case_id].expected_outcome,
                        actual_answer=root_output_answer,
                        judge=judge,
                        case_id=base_case_id,
                    )
                    _log_feedback_safe(
                        name="goal_completion",
                        value=goal.score,
                        trace_id=trace_id,
                        rationale=goal.rationale,
                    )
                    rollup_row["goal_completion"] = goal.score
                    goal_scored += 1
                    print(f"    goal_completion: {goal.score:.2f}")
                except Exception as exc:
                    print(f"    goal_completion failed: {exc}")

        rollup.append(rollup_row)

    print()
    print(
        f"Summary: part1={part1_scored} retrieval={retrieval_scored} "
        f"generation={generation_scored} goal={goal_scored}"
    )

    _write_rollup_markdown(rollup, args.out / "trace_scorer_rollup.md")
    print(f"Wrote {args.out / 'trace_scorer_rollup.md'}")
    return 0


def _write_rollup_markdown(rollup: list[dict[str, object]], path: Path) -> None:
    """Aggregate the per-trace scorer results into a per-case markdown table.

    Groups rows by `case_id`, averaging each numeric metric across a
    scenario's runs. Traces without a case id (production / batch
    traces) land in an "unassigned" section.
    """
    from collections import defaultdict

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rollup:
        cid = str(row.get("case_id") or "(unassigned)")
        grouped[cid].append(row)

    def _mean(rows: list[dict[str, object]], key: str) -> str:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if not values:
            return "-"
        return f"{sum(values) / len(values):.3f}"

    metrics_part1 = ["tool_selection", "trajectory_precision", "trajectory_recall"]
    metrics_gen = ["faithfulness", "answer_relevance", "context_precision", "goal_completion"]

    lines: list[str] = []
    lines.append("# HW4 Part 2 — Trace scorer roll-up\n")
    lines.append(
        "Per-scenario averages of the metrics attached to each trace by "
        "`scripts/run_trace_scorers.py`. Cells with `-` mean the scorer "
        "did not produce a value (empty retrieval, missing answer, or "
        "scorer skipped).\n"
    )
    lines.append(
        "| case_id | runs "
        "| tool_sel | traj_p | traj_r "
        "| faith | rel | ctx_p | goal |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for cid in sorted(grouped):
        rows = grouped[cid]
        row_parts = [cid, str(len(rows))]
        for m in metrics_part1 + metrics_gen:
            row_parts.append(_mean(rows, m))
        lines.append("| " + " | ".join(row_parts) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
