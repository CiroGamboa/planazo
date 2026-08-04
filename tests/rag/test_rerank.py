"""Unit tests for `CrossEncoderReranker`.

The cross-encoder model (`ms-marco-MiniLM-L-6-v2`) is loaded once per
module via a `scope="module"` fixture so the ~90 MB first-run download
is amortized across the file. On a warm HF cache each test costs a few
hundred milliseconds — accepted per the plan's guidance that these are
unit tests running under the default `uv run pytest` suite.
"""

from __future__ import annotations

import pytest

from planazo.rag.models import Chunk, RetrievalResult
from planazo.rag.rerank import CrossEncoderReranker


@pytest.fixture(scope="module")
def reranker() -> CrossEncoderReranker:
    return CrossEncoderReranker()


@pytest.fixture(scope="module")
def rerank_chunks() -> list[Chunk]:
    return [
        Chunk(
            id="paris",
            text="the louvre museum in paris houses the mona lisa and other art",
        ),
        Chunk(
            id="barcelona",
            text="the best barcelona nightlife happens in gothic quarter bars and clubs",
        ),
        Chunk(
            id="recipe",
            text="how to cook the perfect italian pasta with fresh tomato sauce",
        ),
    ]


def test_cross_encoder_reorders_by_query_relevance(
    reranker: CrossEncoderReranker,
    rerank_chunks: list[Chunk],
) -> None:
    result = reranker.rerank("Barcelona nightlife", rerank_chunks, top_k=3)

    assert result.retriever == "reranked"
    assert result.query == "Barcelona nightlife"
    assert len(result.hits) == 3
    assert result.hits[0].chunk_id == "barcelona"
    assert result.hits[0].rank == 1


def test_cross_encoder_empty_chunks_returns_empty_result(reranker: CrossEncoderReranker) -> None:
    result = reranker.rerank("Barcelona nightlife", [], top_k=5)

    assert result == RetrievalResult(query="Barcelona nightlife", hits=[], retriever="reranked")


def test_cross_encoder_rejects_zero_top_k(
    reranker: CrossEncoderReranker,
    rerank_chunks: list[Chunk],
) -> None:
    with pytest.raises(ValueError, match="top_k"):
        reranker.rerank("Barcelona nightlife", rerank_chunks, top_k=0)
