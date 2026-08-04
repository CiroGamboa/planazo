"""Dense + BM25 + RRF hybrid retrieval over a generic list of `Chunk`s.

Domain-agnostic on purpose: these primitives know nothing about events —
they operate over any sequence of `(id, text)` pairs. The event-specific
adapter that projects an `Event` into a scorable document lives in
`planazo.catalog.rag`.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): dense uses
`sentence-transformers/all-MiniLM-L6-v2` with cosine similarity via
L2-normalized dot product, sparse uses `rank_bm25.BM25Okapi`, and the two
ranked lists are fused with Reciprocal Rank Fusion at `k_rrf = 60`
(Cormack et al.). Tied fused scores break by lower `chunk_id` so the
whole pipeline is deterministic on a fixed corpus.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import numpy as np
from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
from sentence_transformers import SentenceTransformer

from planazo.rag.models import Chunk, Hit, RetrievalResult

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fold_accents(text: str) -> str:
    """Return `text` with combining diacritics stripped (NFKD-normalize)."""

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _tokenize(text: str) -> list[str]:
    """Accent-fold + lowercase + word-boundary split on `[a-z0-9]+`."""

    return _TOKEN_RE.findall(_fold_accents(text).lower())


class DenseIndex:
    """In-memory dense retriever over sentence-transformer embeddings.

    Encodes each chunk's `text` once at construction, L2-normalizes the
    embedding matrix, and scores queries via dot product (equivalent to
    cosine similarity on normalized vectors). Ties break by lower
    `chunk_id`.
    """

    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        self._model = SentenceTransformer(model_name)
        self._chunks: list[Chunk] = list(chunks)
        if self._chunks:
            embeddings = self._model.encode(
                [chunk.text for chunk in self._chunks],
                convert_to_numpy=True,
                normalize_embeddings=True,
            ).astype(np.float32)
        else:
            # The model's `get_sentence_embedding_dimension()` gives the
            # right column count, but an empty matrix suffices for search
            # since we short-circuit on empty corpora.
            embeddings = np.zeros((0, self._model.get_sentence_embedding_dimension()), np.float32)
        self._embeddings: np.ndarray = embeddings

    def search(self, query: str, top_n: int) -> RetrievalResult:
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        if not self._chunks:
            return RetrievalResult(query=query, hits=[], retriever="dense")

        query_vec = self._model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype(np.float32)[0]
        scores = self._embeddings @ query_vec
        ranked = _rank_scored_chunks(self._chunks, scores, top_n)
        return RetrievalResult(query=query, hits=ranked, retriever="dense")


class BM25Index:
    """In-memory sparse (BM25 Okapi) retriever.

    Tokenization is `_TOKEN_RE.findall(text.lower())` after NFKD accent
    folding — a simple, language-agnostic split that keeps English,
    Spanish, and Catalan word forms comparable ("Gràcia" folds to "gracia"
    so a query with or without the accent finds the same chunk). Ties
    break by lower `chunk_id`.
    """

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self._chunks: list[Chunk] = list(chunks)
        tokenized_corpus = [_tokenize(chunk.text) for chunk in self._chunks]
        # `BM25Okapi` refuses an empty corpus, so we skip building it when
        # there are no chunks and short-circuit `search` on the same guard.
        self._bm25: BM25Okapi | None = BM25Okapi(tokenized_corpus) if tokenized_corpus else None

    def search(self, query: str, top_n: int) -> RetrievalResult:
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        if self._bm25 is None:
            return RetrievalResult(query=query, hits=[], retriever="bm25")

        scores = np.asarray(self._bm25.get_scores(_tokenize(query)), dtype=np.float32)
        ranked = _rank_scored_chunks(self._chunks, scores, top_n)
        return RetrievalResult(query=query, hits=ranked, retriever="bm25")


def rrf_fuse(
    results: Sequence[RetrievalResult],
    *,
    k_rrf: int = 60,
    top_n: int,
) -> RetrievalResult:
    """Fuse ranked lists via Reciprocal Rank Fusion.

    For each chunk id, the fused score is `Σ 1 / (k_rrf + rank_r(id))`
    across every input result that contains it (missing chunks contribute
    zero). `k_rrf = 60` is the Cormack et al. default; higher values
    flatten the reward for top ranks. Ties break by lower `chunk_id`.

    The shared `query` is inherited from the first result — all input
    results must carry the same query string (a mismatch is a caller bug,
    not a silent merge).
    """

    if k_rrf < 1:
        raise ValueError(f"k_rrf must be >= 1, got {k_rrf}")
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    if not results:
        return RetrievalResult(query="", hits=[], retriever="rrf")

    query = results[0].query
    for result in results[1:]:
        if result.query != query:
            raise ValueError(
                "rrf_fuse requires all inputs to share the same query; "
                f"got {query!r} and {result.query!r}"
            )

    fused_scores: dict[str, float] = {}
    for result in results:
        for hit in result.hits:
            fused_scores[hit.chunk_id] = fused_scores.get(hit.chunk_id, 0.0) + 1.0 / (
                k_rrf + hit.rank
            )

    ordered = sorted(fused_scores.items(), key=lambda pair: (-pair[1], pair[0]))
    top = ordered[:top_n]
    hits = [
        Hit(chunk_id=chunk_id, score=score, rank=idx + 1)
        for idx, (chunk_id, score) in enumerate(top)
    ]
    return RetrievalResult(query=query, hits=hits, retriever="rrf")


class HybridRetriever:
    """Dense + BM25 with RRF fusion — the standard hybrid setup.

    Builds one `DenseIndex` and one `BM25Index` over the same chunks,
    runs both at `.search(...)` time, and returns the RRF-fused top-N.
    Sequential (no threads) — retrieval cost at ~120-chunk corpora is
    tens of milliseconds; concurrency would add complexity without a
    measurable win.
    """

    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        dense_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        k_rrf: int = 60,
    ) -> None:
        if k_rrf < 1:
            raise ValueError(f"k_rrf must be >= 1, got {k_rrf}")
        self._dense = DenseIndex(chunks, model_name=dense_model)
        self._bm25 = BM25Index(chunks)
        self._k_rrf = k_rrf

    def search(self, query: str, *, n_retrieve: int = 20) -> RetrievalResult:
        if n_retrieve < 1:
            raise ValueError(f"n_retrieve must be >= 1, got {n_retrieve}")
        dense_result = self._dense.search(query, n_retrieve)
        bm25_result = self._bm25.search(query, n_retrieve)
        return rrf_fuse([dense_result, bm25_result], k_rrf=self._k_rrf, top_n=n_retrieve)


def _rank_scored_chunks(
    chunks: Sequence[Chunk],
    scores: np.ndarray,
    top_n: int,
) -> list[Hit]:
    """Order `chunks` by descending `scores`, tie-break by `chunk_id`, take top-N."""

    indexed = [(float(scores[i]), chunks[i].id) for i in range(len(chunks))]
    indexed.sort(key=lambda pair: (-pair[0], pair[1]))
    top = indexed[:top_n]
    return [
        Hit(chunk_id=chunk_id, score=score, rank=idx + 1)
        for idx, (score, chunk_id) in enumerate(top)
    ]
