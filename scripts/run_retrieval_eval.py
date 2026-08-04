"""Retrieval eval harness for the RAG-backed `search_events` tool.

Reads the committed seed events + golden queries, runs each query through
`catalog.rag.search_events_rag` at rerank on and rerank off, computes the
five retrieval scorers (`hit@k`, `precision@k`, `recall@k`, MRR, nDCG@k)
at each requested `k`, aggregates the results overall and per failure
category, and writes:

- `data/eval/results/retrieval.md` — markdown tables (overall + per
  failure category).
- `data/eval/results/retrieval_per_case.jsonl` — one line per
  `(case_id, k, rerank)` with all five scores.

The markdown table is also printed to stdout so a smoke run is legible.

Empty-golden cases (the `out_of_corpus` failure category) return `None`
from every scorer; they are excluded from the numeric averages and
counted separately in the output.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from planazo.catalog.models import Event
from planazo.catalog.rag import search_events_rag
from planazo.eval.dataset import GoldenCase, load_golden_cases, load_seed_events
from planazo.eval.metrics import hit_at_k, mrr, ndcg_at_k, precision_at_k, recall_at_k

_DEFAULT_SEED = Path("data/eval/events_seed.jsonl")
_DEFAULT_GOLDEN = Path("data/eval/questions.jsonl")
_DEFAULT_OUT = Path("data/eval/results")
_DEFAULT_KS = (1, 3, 5, 10)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=_DEFAULT_SEED)
    parser.add_argument("--golden", type=Path, default=_DEFAULT_GOLDEN)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=list(_DEFAULT_KS),
        help="k values (space-separated) to score at (default: 1 3 5 10).",
    )
    parser.add_argument(
        "--rerank",
        choices=("on", "off"),
        nargs="+",
        default=["on", "off"],
        help="Rerank settings to sweep (default: on off).",
    )
    return parser.parse_args()


def _run_query(events: list[Event], query: str, *, rerank: bool, top_k: int) -> list[str]:
    """Return the retrieved event ids in ranked order, longest first.

    The harness asks for `top_k = max(k values)` so slicing by any smaller
    `k` at scoring time is position-aware over the retriever's ranking.
    """
    retrieved = search_events_rag(events, query, rerank=rerank, k=top_k, n_retrieve=max(20, top_k))
    return [str(event.id) for event in retrieved]


def _score_case(retrieved_ids: list[str], golden_ids: list[str], k: int) -> dict[str, float | None]:
    return {
        "hit_at_k": hit_at_k(retrieved_ids, golden_ids, k),
        "precision_at_k": precision_at_k(retrieved_ids, golden_ids, k),
        "recall_at_k": recall_at_k(retrieved_ids, golden_ids, k),
        "mrr": mrr(retrieved_ids, golden_ids),
        "ndcg_at_k": ndcg_at_k(retrieved_ids, golden_ids, k),
    }


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _render_number(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def _render_overall_table(aggregates: dict[tuple[str, int, bool], dict[str, float | None]]) -> str:
    """Build the overall averages markdown table."""
    rerank_labels = {True: "on", False: "off"}
    metric_names = ("hit_at_k", "precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")

    lines = [
        "| k | rerank | " + " | ".join(metric_names) + " |",
        "| --- | --- | " + " | ".join(["---"] * len(metric_names)) + " |",
    ]
    # Deterministic iteration: k asc, rerank on first.
    ordered_keys: list[tuple[str, int, bool]] = []
    for k in sorted({key[1] for key in aggregates}):
        for rerank_bool in (True, False):
            key = ("overall", k, rerank_bool)
            if key in aggregates:
                ordered_keys.append(key)
    for key in ordered_keys:
        _, k, rerank = key
        row = aggregates[key]
        cells = [_render_number(row[name]) for name in metric_names]
        lines.append(f"| {k} | {rerank_labels[rerank]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _render_per_category_table(
    aggregates: dict[tuple[str, int, bool], dict[str, float | None]],
) -> str:
    rerank_labels = {True: "on", False: "off"}
    metric_names = ("hit_at_k", "precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")

    categories = sorted({key[0] for key in aggregates if key[0] != "overall"})
    lines = [
        "| failure_category | k | rerank | " + " | ".join(metric_names) + " |",
        "| --- | --- | --- | " + " | ".join(["---"] * len(metric_names)) + " |",
    ]
    for category in categories:
        for k in sorted({key[1] for key in aggregates if key[0] == category}):
            for rerank_bool in (True, False):
                key = (category, k, rerank_bool)
                if key not in aggregates:
                    continue
                row = aggregates[key]
                cells = [_render_number(row[name]) for name in metric_names]
                lines.append(
                    f"| {category} | {k} | {rerank_labels[rerank_bool]} | "
                    + " | ".join(cells)
                    + " |"
                )
    return "\n".join(lines) + "\n"


def _aggregate(
    per_case_rows: list[dict[str, Any]],
) -> dict[tuple[str, int, bool], dict[str, float | None]]:
    """Bucket per-case scores by (scope, k, rerank), take the mean per metric."""
    metric_names = ("hit_at_k", "precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")
    buckets: dict[tuple[str, int, bool], dict[str, list[float]]] = {}
    for row in per_case_rows:
        scope_keys = [("overall", row["k"], row["rerank"])]
        scope_keys.append((row["failure_category"], row["k"], row["rerank"]))
        for scope in scope_keys:
            metrics_bucket = buckets.setdefault(scope, {name: [] for name in metric_names})
            for name in metric_names:
                score = row["scores"][name]
                if score is not None:
                    metrics_bucket[name].append(score)
    return {
        scope: {name: _mean(values) for name, values in metric_map.items()}
        for scope, metric_map in buckets.items()
    }


def _count_empty_golden(cases: list[GoldenCase]) -> int:
    return sum(1 for case in cases if not case.golden_event_ids)


def main() -> int:
    args = _parse_args()

    events = load_seed_events(args.seed)
    cases = load_golden_cases(args.golden)
    rerank_flags = [flag == "on" for flag in args.rerank]
    top_k = max(args.k)

    per_case_rows: list[dict[str, Any]] = []
    for rerank in rerank_flags:
        for case in cases:
            retrieved_ids = _run_query(events, case.query, rerank=rerank, top_k=top_k)
            for k in args.k:
                scores = _score_case(retrieved_ids, case.golden_event_ids, k)
                per_case_rows.append(
                    {
                        "case_id": case.id,
                        "failure_category": case.failure_category,
                        "query": case.query,
                        "k": k,
                        "rerank": rerank,
                        "retrieved_ids_top_k": retrieved_ids[:k],
                        "golden_event_ids": case.golden_event_ids,
                        "scores": scores,
                    }
                )

    aggregates = _aggregate(per_case_rows)
    empty_golden_count = _count_empty_golden(cases)

    args.out.mkdir(parents=True, exist_ok=True)
    per_case_path = args.out / "retrieval_per_case.jsonl"
    markdown_path = args.out / "retrieval.md"

    with per_case_path.open("w", encoding="utf-8") as handle:
        for row in per_case_rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    header = (
        "# Retrieval eval — RAG-backed `search_events`\n\n"
        f"- Seed corpus: `{args.seed}` ({len(events)} events)\n"
        f"- Golden cases: `{args.golden}` ({len(cases)} cases)\n"
        f"- Empty-golden (`out_of_corpus`) cases excluded from means: "
        f"**{empty_golden_count}**\n"
        f"- k sweep: {args.k}; rerank sweep: {args.rerank}\n\n"
        "## Overall averages\n\n"
    )
    overall_table = _render_overall_table(aggregates)
    per_cat_header = "\n## Per failure_category\n\n"
    per_cat_table = _render_per_category_table(aggregates)

    document = header + overall_table + per_cat_header + per_cat_table
    markdown_path.write_text(document, encoding="utf-8")

    # Also print the overall table to stdout for a legible smoke run.
    print(document)
    print(f"\nWrote {markdown_path} and {per_case_path}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
