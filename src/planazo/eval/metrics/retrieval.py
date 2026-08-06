"""Hand-rolled retrieval scorers — `hit@k`, `precision@k`, `recall@k`, MRR, nDCG@k.

Each scorer takes the retriever's ranked `retrieved_ids` (position-aware, in
descending relevance order) and the case's `golden_ids`, and returns a float
in `[0.0, 1.0]`. Every scorer returns `None` when `golden_ids` is empty — the
harness aggregates non-`None` scores into the mean and counts the empty
cases separately, so an out-of-corpus case can never poison an average.

Per [ADR 0025](../../../../../docs/adr/0025-rag-over-events.md): scorers are
hand-rolled rather than delegated to Ragas or DeepEval — the runtime
dependency graph stays small, the empty-golden semantics are ours, and the
formulas are the ones the report cites.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _validate_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")


def hit_at_k(retrieved_ids: Sequence[str], golden_ids: Sequence[str], k: int) -> float | None:
    """`1.0` if any golden id appears in the top-`k` retrieved; `0.0` otherwise.

    Returns `None` when `golden_ids` is empty — the empty-golden branch the
    harness segregates from the numeric aggregate.
    """
    _validate_k(k)
    if not golden_ids:
        return None
    golden_set = set(golden_ids)
    top_k = retrieved_ids[:k]
    return 1.0 if any(chunk_id in golden_set for chunk_id in top_k) else 0.0


def precision_at_k(retrieved_ids: Sequence[str], golden_ids: Sequence[str], k: int) -> float | None:
    """`(# golden ids in top-k) / k`.

    Returns `None` when `golden_ids` is empty.
    """
    _validate_k(k)
    if not golden_ids:
        return None
    golden_set = set(golden_ids)
    top_k = retrieved_ids[:k]
    hits = sum(1 for chunk_id in top_k if chunk_id in golden_set)
    return hits / k


def recall_at_k(retrieved_ids: Sequence[str], golden_ids: Sequence[str], k: int) -> float | None:
    """`(# golden ids in top-k) / (# golden ids)`.

    Returns `None` when `golden_ids` is empty.
    """
    _validate_k(k)
    if not golden_ids:
        return None
    golden_set = set(golden_ids)
    top_k = retrieved_ids[:k]
    hits = sum(1 for chunk_id in top_k if chunk_id in golden_set)
    return hits / len(golden_set)


def mrr(retrieved_ids: Sequence[str], golden_ids: Sequence[str]) -> float | None:
    """Reciprocal rank of the first golden hit; `0.0` if none is present.

    Returns `None` when `golden_ids` is empty. Position-aware — the rank of
    the first golden id follows the retriever's raw order, no repacking.
    """
    if not golden_ids:
        return None
    golden_set = set(golden_ids)
    for position, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in golden_set:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved_ids: Sequence[str], golden_ids: Sequence[str], k: int) -> float | None:
    """Binary-relevance nDCG@k: `DCG@k / IDCG@k`.

    Each golden id in the top-`k` contributes `1 / log2(1 + rank)` to the DCG
    (relevance is `1`, everything else is `0`). `IDCG@k` is the DCG of the
    ideal ranking that packs `min(k, |golden|)` golden ids into positions
    `1..min(k, |golden|)`. Returns `None` when `golden_ids` is empty.
    """
    _validate_k(k)
    if not golden_ids:
        return None
    golden_set = set(golden_ids)
    dcg = 0.0
    for position, chunk_id in enumerate(retrieved_ids[:k], start=1):
        if chunk_id in golden_set:
            dcg += 1.0 / math.log2(1 + position)
    ideal_hits = min(k, len(golden_set))
    idcg = sum(1.0 / math.log2(1 + position) for position in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg
