"""Generation eval harness for the RAG-backed `search_events` tool.

Reads the committed seed events + golden queries, runs each query through
`catalog.rag.search_events_rag`, composes a deterministic "answer" from the
retrieved events (no LLM call — the harness measures RAG + retrieval-
formatting quality, not agent-response quality), then asks an
`LLMJudge` (an `OpenCodeJudge` by default) to score three generation
metrics: Faithfulness, Answer Relevance, Context Precision.

Every judge decision is disk-cached under `var/eval/judge_cache/` so a
second run of the harness costs zero LLM calls. AGENTS.md rule 4 stays
satisfied — a malformed judge reply is a typed fallback
(`score=0.0, rationale="judge_parse_failed: ..."`), never a silent success.

The rerank-on sweep covers the full 20-case golden set; the rerank-off
sweep covers a deterministically selected 10-case subset balanced across
failure categories. The subset selection algorithm and the chosen case
ids are recorded to `data/eval/results/generation_subset.md` for
reproducibility.

Outputs (under `--out`):
- `generation.md` — markdown table (overall + per failure category).
- `generation_per_case.jsonl` — one JSON line per case with all scores +
  rationale snippets.
- `generation_subset.md` — the 10 case ids selected for rerank-off.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import planazo.config  # noqa: F401 — imported for the `.env` side-effect
from planazo.catalog.models import Event
from planazo.catalog.rag import event_to_document, search_events_rag
from planazo.eval.dataset import GoldenCase, load_golden_cases, load_seed_events
from planazo.eval.judge import LLMJudge, OpenCodeJudge
from planazo.eval.metrics import (
    score_answer_relevance,
    score_context_precision,
    score_faithfulness,
)

_DEFAULT_SEED = Path("data/eval/events_seed.jsonl")
_DEFAULT_GOLDEN = Path("data/eval/questions.jsonl")
_DEFAULT_OUT = Path("data/eval/results")
_DEFAULT_CACHE = Path("var/eval/judge_cache")

_METRICS = ("faithfulness", "answer_relevance", "context_precision")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=_DEFAULT_SEED)
    parser.add_argument("--golden", type=Path, default=_DEFAULT_GOLDEN)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--cache", type=Path, default=_DEFAULT_CACHE)
    parser.add_argument(
        "--rerank",
        choices=("on", "off"),
        nargs="+",
        default=["on", "off"],
        help="Rerank settings to sweep (default: on off).",
    )
    parser.add_argument(
        "--rerank-off-subset",
        type=int,
        default=10,
        help="Number of cases to score with rerank off, balanced across failure "
        "categories (default: 10).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Judge model id (defaults to the CHEAP role via OpenCodeJudge).",
    )
    parser.add_argument(
        "--disable-judge",
        action="store_true",
        help="Do not call the judge model — every uncached decision falls back "
        "to score=0.0 with a `judge_parse_failed` rationale. Useful for a "
        "smoke run that only exercises the harness plumbing.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Top-k depth to retrieve per case (default: 5).",
    )
    return parser.parse_args()


def format_events_as_answer(events: Sequence[Event]) -> str:
    """Compose one deterministic answer string from a ranked event list.

    Each event renders as `"{title} — {description[:80]}… at {venue_name}. "
    "{start_utc}."` and the lines are joined with `\n`. This is a synthetic
    answer built purely from retrieved chunks — the judge grades whether
    the retrieval supports the golden answer, not whether an LLM composed a
    fluent paragraph. Documented in the report at Stage 6.
    """
    lines: list[str] = []
    for event in events:
        description = (event.description or "").strip()
        if len(description) > 80:
            description = description[:80] + "…"
        venue = event.venue_name or "Barcelona"
        lines.append(f"{event.title} — {description} at {venue}. {event.start_utc.isoformat()}.")
    return "\n".join(lines)


def select_rerank_off_subset(cases: list[GoldenCase], target_size: int) -> list[GoldenCase]:
    """Select `target_size` cases balanced across failure categories.

    Algorithm (deterministic):
    1. Group cases by `failure_category`, then sort each group by `id`.
    2. Allocate `ceil(target_size / num_categories)` slots per category —
       every category is represented (rounding up avoids the
       under-represented-category-drops-out failure mode).
    3. Walk the categories in sorted order and pull the first N cases from
       each group. If the aggregate exceeds `target_size` (because rounding
       up over-allocates), trim from the tail categories.

    Because every step is a sort or a slice, the same input always yields
    the same subset — the report can name specific case ids.
    """
    if target_size <= 0:
        return []
    by_category: dict[str, list[GoldenCase]] = defaultdict(list)
    for case in cases:
        by_category[case.failure_category].append(case)
    for group in by_category.values():
        group.sort(key=lambda case: case.id)

    categories = sorted(by_category)
    per_category = math.ceil(target_size / len(categories))
    picked: list[GoldenCase] = []
    for category in categories:
        picked.extend(by_category[category][:per_category])
    # Deterministic trim: iterate categories in reverse until we hit the target.
    while len(picked) > target_size:
        for category in reversed(categories):
            hits = [case for case in picked if case.failure_category == category]
            if len(hits) > 1:
                picked.remove(hits[-1])
                break
        else:
            break
    picked.sort(key=lambda case: case.id)
    return picked


def _run_case(
    *,
    case: GoldenCase,
    events: list[Event],
    rerank: bool,
    top_k: int,
    judge: LLMJudge,
) -> dict[str, Any]:
    """Retrieve, compose an answer, score three metrics, and return one row."""
    retrieved = search_events_rag(events, case.query, rerank=rerank, k=top_k, n_retrieve=20)
    answer = format_events_as_answer(retrieved)
    chunks = [event_to_document(event) for event in retrieved]

    faithfulness = score_faithfulness(answer=answer, chunks=chunks, judge=judge, case_id=case.id)
    answer_relevance = score_answer_relevance(
        query=case.query, answer=answer, judge=judge, case_id=case.id
    )
    context_precision = score_context_precision(
        query=case.query, chunks=chunks, judge=judge, case_id=case.id
    )
    return {
        "case_id": case.id,
        "failure_category": case.failure_category,
        "query": case.query,
        "rerank": rerank,
        "retrieved_ids": [str(event.id) for event in retrieved],
        "golden_event_ids": case.golden_event_ids,
        "scores": {
            "faithfulness": faithfulness.score,
            "answer_relevance": answer_relevance.score,
            "context_precision": context_precision.score,
        },
        "rationales": {
            "faithfulness": faithfulness.rationale[:200],
            "answer_relevance": answer_relevance.rationale[:200],
            "context_precision": context_precision.rationale[:200],
        },
    }


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _render_number(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def _aggregate(
    per_case_rows: list[dict[str, Any]],
) -> dict[tuple[str, bool], dict[str, float | None]]:
    """Bucket per-case scores by (scope, rerank) and take the mean per metric.

    Scope is either `"overall"` or the case's `failure_category`.
    """
    buckets: dict[tuple[str, bool], dict[str, list[float]]] = {}
    for row in per_case_rows:
        rerank = row["rerank"]
        for scope in ("overall", row["failure_category"]):
            metric_map = buckets.setdefault((scope, rerank), {name: [] for name in _METRICS})
            for name in _METRICS:
                score = row["scores"][name]
                if score is not None:
                    metric_map[name].append(score)
    return {
        scope: {name: _mean(values) for name, values in metric_map.items()}
        for scope, metric_map in buckets.items()
    }


def _render_overall_table(aggregates: dict[tuple[str, bool], dict[str, float | None]]) -> str:
    lines = [
        "| rerank | " + " | ".join(_METRICS) + " |",
        "| --- | " + " | ".join(["---"] * len(_METRICS)) + " |",
    ]
    for rerank in (True, False):
        key = ("overall", rerank)
        if key not in aggregates:
            continue
        cells = [_render_number(aggregates[key][name]) for name in _METRICS]
        lines.append(f"| {'on' if rerank else 'off'} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _render_per_category_table(
    aggregates: dict[tuple[str, bool], dict[str, float | None]],
) -> str:
    lines = [
        "| failure_category | rerank | " + " | ".join(_METRICS) + " |",
        "| --- | --- | " + " | ".join(["---"] * len(_METRICS)) + " |",
    ]
    categories = sorted({key[0] for key in aggregates if key[0] != "overall"})
    for category in categories:
        for rerank in (True, False):
            key = (category, rerank)
            if key not in aggregates:
                continue
            cells = [_render_number(aggregates[key][name]) for name in _METRICS]
            lines.append(
                f"| {category} | {'on' if rerank else 'off'} | " + " | ".join(cells) + " |"
            )
    return "\n".join(lines) + "\n"


def _write_subset_manifest(path: Path, subset: list[GoldenCase], target_size: int) -> None:
    body = [
        "# Rerank-off subset\n",
        (
            f"Selected {len(subset)} of {target_size} target cases via a "
            "deterministic balanced-across-failure-categories algorithm "
            "(`select_rerank_off_subset`). The rerank-off numbers in "
            "`generation.md` are the mean over exactly these case ids.\n"
        ),
        "| case_id | failure_category | query |",
        "| --- | --- | --- |",
    ]
    for case in subset:
        query_escaped = case.query.replace("|", "\\|")
        body.append(f"| {case.id} | {case.failure_category} | {query_escaped} |")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()

    events = load_seed_events(args.seed)
    cases = load_golden_cases(args.golden)
    rerank_flags = [flag == "on" for flag in args.rerank]

    cache_root: Path = args.cache
    cache_root.mkdir(parents=True, exist_ok=True)
    enabled = not args.disable_judge
    if args.model is not None:
        judge: LLMJudge = OpenCodeJudge(
            cache_root=cache_root, model_role=args.model, enabled=enabled
        )
    else:
        judge = OpenCodeJudge(cache_root=cache_root, enabled=enabled)

    subset = (
        select_rerank_off_subset(cases, args.rerank_off_subset) if False in rerank_flags else []
    )

    per_case_rows: list[dict[str, Any]] = []
    for rerank in rerank_flags:
        selected = cases if rerank else subset
        for case in selected:
            row = _run_case(
                case=case,
                events=events,
                rerank=rerank,
                top_k=args.k,
                judge=judge,
            )
            per_case_rows.append(row)

    aggregates = _aggregate(per_case_rows)

    args.out.mkdir(parents=True, exist_ok=True)
    per_case_path = args.out / "generation_per_case.jsonl"
    markdown_path = args.out / "generation.md"
    subset_path = args.out / "generation_subset.md"

    with per_case_path.open("w", encoding="utf-8") as handle:
        for row in per_case_rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    if subset:
        _write_subset_manifest(subset_path, subset, args.rerank_off_subset)

    header = (
        "# Generation eval — RAG-backed `search_events`\n\n"
        f"- Seed corpus: `{args.seed}` ({len(events)} events)\n"
        f"- Golden cases: `{args.golden}` ({len(cases)} cases)\n"
        f"- Rerank sweep: {args.rerank}; rerank-off subset size: "
        f"{len(subset) if False in rerank_flags else 0}\n"
        f"- Judge: `OpenCodeJudge` "
        f"(model={args.model or 'CHEAP role'}, enabled={not args.disable_judge})\n"
        f"- Cache root: `{cache_root}`\n\n"
        "## Overall averages\n\n"
    )
    overall_table = _render_overall_table(aggregates)
    per_cat_header = "\n## Per failure_category\n\n"
    per_cat_table = _render_per_category_table(aggregates)

    document = header + overall_table + per_cat_header + per_cat_table
    markdown_path.write_text(document, encoding="utf-8")

    print(document)
    trailing = f"\nWrote {markdown_path} and {per_case_path}"
    if subset:
        trailing += f" and {subset_path}"
    print(trailing + ".", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
