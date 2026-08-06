"""Cross-encoder reranker — reorders a retrieved candidate set by query relevance.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): the reranker
runs after RRF fusion has narrowed the corpus to ~20 candidates, then trims
to top-K (default 5). A cross-encoder scores each `(query, chunk_text)`
pair jointly rather than as two separate embeddings, which typically yields
better ordering at higher latency than a bi-encoder like the dense index —
worthwhile only over a small candidate set.
"""

from __future__ import annotations

from collections.abc import Sequence

from sentence_transformers import CrossEncoder

from planazo.rag.models import Chunk, Hit, RetrievalResult


class CrossEncoderReranker:
    """Sentence-transformers cross-encoder that reorders `Chunk`s by relevance.

    Unlike a bi-encoder (`DenseIndex`), a cross-encoder scores `(query,
    document)` pairs jointly — the two texts share the encoder's attention
    layers, which is more expensive but usually more accurate. Scores from
    the `ms-marco` family are unbounded logits (roughly [-10, +10]) — do
    not compare them to the [-1, +1] cosine similarities `DenseIndex`
    returns. Only ordering within a single rerank call is meaningful.
    """

    def __init__(
        self,
        *,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    ) -> None:
        self._model = CrossEncoder(model_name)

    def rerank(
        self,
        query: str,
        chunks: Sequence[Chunk],
        *,
        top_k: int,
    ) -> RetrievalResult:
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        chunk_list = list(chunks)
        if not chunk_list:
            return RetrievalResult(query=query, hits=[], retriever="reranked")

        raw_scores = self._model.predict([(query, chunk.text) for chunk in chunk_list])
        indexed = [(float(raw_scores[i]), chunk_list[i].id) for i in range(len(chunk_list))]
        indexed.sort(key=lambda pair: (-pair[0], pair[1]))
        top = indexed[:top_k]
        hits = [
            Hit(chunk_id=chunk_id, score=score, rank=idx + 1)
            for idx, (score, chunk_id) in enumerate(top)
        ]
        return RetrievalResult(query=query, hits=hits, retriever="reranked")
