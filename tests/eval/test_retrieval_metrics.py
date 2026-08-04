"""Unit tests for the five hand-rolled retrieval scorers.

Each scorer is covered by (a) a golden-hit fixture, (b) a golden-miss
fixture, (c) the empty-golden branch that returns `None`, and — for the
`@k` scorers — a `k=0` invalidation. `ndcg_at_k` also gets an
ordering-sensitivity check.

Retrieval scorers are pure functions of the ranked-id list; there is no
model to load and no I/O to mock, so these tests run in a few
milliseconds each.
"""

from __future__ import annotations

import math

import pytest

from planazo.eval.metrics import hit_at_k, mrr, ndcg_at_k, precision_at_k, recall_at_k


def test_hit_at_k_returns_1_when_golden_in_top_k() -> None:
    assert hit_at_k(["a", "b", "c", "d"], ["c"], k=3) == 1.0


def test_hit_at_k_returns_0_when_no_golden_in_top_k() -> None:
    assert hit_at_k(["a", "b", "c", "d"], ["e"], k=3) == 0.0


def test_hit_at_k_returns_none_on_empty_golden() -> None:
    assert hit_at_k(["a", "b", "c"], [], k=3) is None


def test_hit_at_k_rejects_zero_k() -> None:
    with pytest.raises(ValueError, match="k"):
        hit_at_k(["a", "b"], ["a"], k=0)


def test_precision_at_k_returns_fraction_of_hits_in_top_k() -> None:
    # top-4 = [a, b, c, d]; two of them (a, c) are golden → 2/4 = 0.5.
    assert precision_at_k(["a", "b", "c", "d"], ["a", "c", "z"], k=4) == 0.5


def test_precision_at_k_returns_0_when_no_hits() -> None:
    assert precision_at_k(["a", "b", "c"], ["z"], k=3) == 0.0


def test_precision_at_k_returns_none_on_empty_golden() -> None:
    assert precision_at_k(["a", "b", "c"], [], k=3) is None


def test_precision_at_k_rejects_zero_k() -> None:
    with pytest.raises(ValueError, match="k"):
        precision_at_k(["a"], ["a"], k=0)


def test_recall_at_k_returns_fraction_of_golden_recovered() -> None:
    # golden = {a, b, c}; top-2 = [a, b] recovers 2 of 3 → 2/3.
    result = recall_at_k(["a", "b", "z", "y"], ["a", "b", "c"], k=2)
    assert result is not None
    assert math.isclose(result, 2 / 3)


def test_recall_at_k_returns_0_when_no_hits() -> None:
    assert recall_at_k(["x", "y", "z"], ["a", "b"], k=3) == 0.0


def test_recall_at_k_returns_none_on_empty_golden() -> None:
    assert recall_at_k(["a", "b"], [], k=3) is None


def test_recall_at_k_rejects_zero_k() -> None:
    with pytest.raises(ValueError, match="k"):
        recall_at_k(["a"], ["a"], k=0)


def test_mrr_returns_reciprocal_of_first_golden_rank() -> None:
    # First golden at rank 3 → 1/3.
    result = mrr(["a", "b", "c"], ["c"])
    assert result is not None
    assert math.isclose(result, 1 / 3)


def test_mrr_returns_1_when_first_hit_at_rank_1() -> None:
    assert mrr(["c", "a", "b"], ["c"]) == 1.0


def test_mrr_returns_zero_when_no_golden_present() -> None:
    assert mrr(["a", "b", "c"], ["d"]) == 0.0


def test_mrr_returns_none_on_empty_golden() -> None:
    assert mrr(["a", "b", "c"], []) is None


def test_ndcg_at_k_perfect_ranking_scores_1() -> None:
    result = ndcg_at_k(["a", "b", "c"], ["a", "b", "c"], k=3)
    assert result is not None
    assert math.isclose(result, 1.0)


def test_ndcg_at_k_reverse_ranking_scores_below_1() -> None:
    # golden = {a, b, c}; retrieved reversed → still all hits in top-3 so
    # nDCG@3 == 1.0 (all three positions carry a golden id). Test the
    # partial-ordering case instead: one hit in a later position.
    forward = ndcg_at_k(["a", "b", "c"], ["a"], k=3)
    backward = ndcg_at_k(["c", "b", "a"], ["a"], k=3)
    assert forward is not None
    assert backward is not None
    assert math.isclose(forward, 1.0)
    assert backward < 1.0


def test_ndcg_at_k_returns_0_when_no_hits_in_top_k() -> None:
    assert ndcg_at_k(["x", "y", "z"], ["a"], k=3) == 0.0


def test_ndcg_at_k_returns_none_on_empty_golden() -> None:
    assert ndcg_at_k(["a", "b", "c"], [], k=3) is None


def test_ndcg_at_k_rejects_zero_k() -> None:
    with pytest.raises(ValueError, match="k"):
        ndcg_at_k(["a"], ["a"], k=0)
