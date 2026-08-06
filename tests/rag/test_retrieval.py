"""Unit tests for the domain-agnostic retrieval primitives.

Each retriever fixture loads the underlying model at most once per test
module (via `scope="module"`) so a first-run download of the ~90 MB
`all-MiniLM-L6-v2` weights is amortized across the whole file rather
than paid per test.
"""

from __future__ import annotations

from typing import Literal

import pytest

from planazo.rag.models import Chunk, Hit, RetrievalResult
from planazo.rag.retrieval import BM25Index, DenseIndex, HybridRetriever, rrf_fuse


@pytest.fixture(scope="module")
def semantic_chunks() -> list[Chunk]:
    return [
        Chunk(id="0", text="python programming language"),
        Chunk(id="1", text="the weather in paris"),
        Chunk(id="2", text="cooking pasta recipe"),
    ]


@pytest.fixture(scope="module")
def multilingual_chunks() -> list[Chunk]:
    return [
        Chunk(id="0", text="concierto de flamenco a Gràcia"),
        Chunk(id="1", text="electronic music in Poble Sec"),
        Chunk(id="2", text="rooftop cocktails in Eixample"),
    ]


@pytest.fixture(scope="module")
def dense_index(semantic_chunks: list[Chunk]) -> DenseIndex:
    return DenseIndex(semantic_chunks)


@pytest.fixture(scope="module")
def bm25_index(semantic_chunks: list[Chunk]) -> BM25Index:
    return BM25Index(semantic_chunks)


def test_dense_index_ranks_matching_chunk_first(dense_index: DenseIndex) -> None:
    result = dense_index.search("python code", top_n=3)

    assert result.retriever == "dense"
    assert result.query == "python code"
    assert len(result.hits) == 3
    assert result.hits[0].chunk_id == "0"
    assert result.hits[0].rank == 1


def test_dense_index_empty_corpus_returns_empty() -> None:
    index = DenseIndex([])

    result = index.search("anything", top_n=5)

    assert result == RetrievalResult(query="anything", hits=[], retriever="dense")


def test_dense_index_rejects_zero_top_n(dense_index: DenseIndex) -> None:
    with pytest.raises(ValueError, match="top_n"):
        dense_index.search("python code", top_n=0)


def test_bm25_index_ranks_exact_term_match_first(bm25_index: BM25Index) -> None:
    result = bm25_index.search("pasta", top_n=3)

    assert result.retriever == "bm25"
    assert result.hits[0].chunk_id == "2"
    assert result.hits[0].rank == 1


def test_bm25_index_empty_corpus_returns_empty() -> None:
    index = BM25Index([])

    result = index.search("anything", top_n=5)

    assert result == RetrievalResult(query="anything", hits=[], retriever="bm25")


def test_bm25_index_rejects_zero_top_n(bm25_index: BM25Index) -> None:
    with pytest.raises(ValueError, match="top_n"):
        bm25_index.search("pasta", top_n=0)


def test_bm25_tokenizer_handles_spanish_catalan_words(multilingual_chunks: list[Chunk]) -> None:
    index = BM25Index(multilingual_chunks)

    accented_hit = index.search("flamenco", top_n=3).hits[0]
    assert accented_hit.chunk_id == "0"

    # Accent-folding: querying "Gracia" (no accent) still hits the
    # "Gràcia" chunk because the tokenizer NFKD-normalizes both sides.
    folded_hit = index.search("Gracia", top_n=3).hits[0]
    assert folded_hit.chunk_id == "0"


RetrieverName = Literal["dense", "bm25", "rrf", "reranked"]


def _mk_result(order: list[str], *, retriever: RetrieverName = "dense") -> RetrievalResult:
    hits = [
        Hit(chunk_id=chunk_id, score=1.0 / (i + 1), rank=i + 1) for i, chunk_id in enumerate(order)
    ]
    return RetrievalResult(query="q", hits=hits, retriever=retriever)


def test_rrf_fuse_ties_broken_deterministically() -> None:
    order = ["a", "b", "c"]
    fused_same = rrf_fuse(
        [_mk_result(order), _mk_result(order, retriever="bm25")],
        top_n=3,
    )
    assert [hit.chunk_id for hit in fused_same.hits] == order

    swapped = ["b", "a", "c"]
    fused_swap = rrf_fuse(
        [_mk_result(order), _mk_result(swapped, retriever="bm25")],
        top_n=3,
    )
    # a: 1/(60+1) + 1/(60+2), b: 1/(60+2) + 1/(60+1) → tied, tiebreak by id
    assert [hit.chunk_id for hit in fused_swap.hits[:2]] == ["a", "b"]
    assert fused_swap.hits[2].chunk_id == "c"


def test_rrf_fuse_rejects_mismatched_queries() -> None:
    left = _mk_result(["a"])
    right = RetrievalResult(
        query="different",
        hits=[Hit(chunk_id="a", score=1.0, rank=1)],
        retriever="bm25",
    )

    with pytest.raises(ValueError, match="same query"):
        rrf_fuse([left, right], top_n=1)


def test_rrf_fuse_rejects_bad_k_rrf() -> None:
    with pytest.raises(ValueError, match="k_rrf"):
        rrf_fuse([_mk_result(["a"])], k_rrf=0, top_n=1)


def test_rrf_fuse_promotes_chunk_present_in_both() -> None:
    # Chunk A at rank 5 in dense, rank 4 in bm25 → present in both.
    # Chunk B at rank 1 in dense only → extreme-in-one.
    dense_order = ["B", "x1", "x2", "x3", "A"]
    bm25_order = ["y1", "y2", "y3", "A"] + [f"z{i}" for i in range(16)]

    fused = rrf_fuse(
        [_mk_result(dense_order), _mk_result(bm25_order, retriever="bm25")],
        top_n=5,
    )

    ids = [hit.chunk_id for hit in fused.hits]
    assert ids.index("A") < ids.index("B")


def test_rrf_fuse_empty_input_returns_empty() -> None:
    result = rrf_fuse([], top_n=5)
    assert result == RetrievalResult(query="", hits=[], retriever="rrf")


def test_hybrid_retriever_returns_top_n(semantic_chunks: list[Chunk]) -> None:
    extras = [
        *semantic_chunks,
        Chunk(id="3", text="jazz concert tonight"),
        Chunk(id="4", text="museum tour saturday"),
        Chunk(id="5", text="tapas bar walking tour"),
    ]

    retriever = HybridRetriever(extras)
    result = retriever.search("python code", n_retrieve=3)

    assert result.retriever == "rrf"
    assert len(result.hits) == 3
    assert result.query == "python code"
