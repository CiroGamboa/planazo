"""Domain-agnostic retrieval primitives for the Recommender's RAG-backed tools.

Owns the retrieval mechanics that are independent of any particular Planazo
aggregate: the Pydantic models for chunks and hits, the dense (sentence-
transformers) + sparse (BM25) indexes, the Reciprocal Rank Fusion combiner
that merges their ranked lists, and the cross-encoder reranker that reorders
the fused top-N down to top-K. The event-domain glue that turns an `Event`
into a scorable document lives one context over in `planazo.catalog.rag`.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): local models
only (`all-MiniLM-L6-v2` for dense, `ms-marco-MiniLM-L-6-v2` for rerank),
one chunk per event, `k_rrf = 60`, retrieve depth 20, return depth 5, and a
`rerank: bool` seam so the evaluation harness can measure the reranker's
contribution in isolation.
"""

from planazo.rag.models import Chunk, Hit, RetrievalResult
from planazo.rag.rerank import CrossEncoderReranker
from planazo.rag.retrieval import BM25Index, DenseIndex, HybridRetriever, rrf_fuse

__all__ = [
    "BM25Index",
    "Chunk",
    "CrossEncoderReranker",
    "DenseIndex",
    "Hit",
    "HybridRetriever",
    "RetrievalResult",
    "rrf_fuse",
]
