"""Event-domain glue for the RAG-backed `search_events` tool.

Bridges the domain-agnostic retrieval primitives in `planazo.rag` to the
event aggregate that lives here: turns an `Event` into the deterministic
document string the retriever scores, and exposes
`search_events_rag(events, query, *, rerank, k)` — the identity-closed
factory the Recommender's inline registration in `event_agent.run_once`
calls when a natural-language `query` is present.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): one chunk
per event (the row is the atomic semantic unit), chunk id = event id, and
hard filters (`category`, `city`, date window, `price_max`) still gate the
candidate set before RAG ranks within it. Backward compatible for callers
that omit `query`.
"""

from __future__ import annotations

from collections.abc import Sequence

from planazo.catalog.models import Event
from planazo.rag import (
    Chunk,
    CrossEncoderReranker,
    HybridRetriever,
)


def event_to_document(event: Event) -> str:
    """Project one `Event` onto the deterministic document string the retriever scores.

    Format:
    `"{title}. {description}. Venue: {venue_name} at {venue_address}. `
    `Category: {category}. Tags: {tags}. Time: {start_utc}. Price: {price}"`

    `None`/empty optional fields collapse to empty strings so the projection
    never crashes on partial rows. Tags render as a comma-joined list;
    `price_cents == 0` renders as `"free"`, otherwise as the raw cent value.

    ADR 0025's proposed projection included a `Neighborhood:` phrase with a
    `"Barcelona"` fallback; the current `Event` schema has no `neighborhood`
    field, so the phrase is omitted here. Adding one would be a schema
    change and a future ADR — the fallback intent is preserved implicitly by
    the `city` field, which every row carries.
    """

    title = event.title
    description = event.description or ""
    venue_name = event.venue_name or ""
    venue_address = event.venue_address or ""
    category = event.category
    tags = ", ".join(event.tags) if event.tags else ""
    time = str(event.start_utc)
    price = "free" if event.price_cents == 0 else str(event.price_cents)
    return (
        f"{title}. {description}. "
        f"Venue: {venue_name} at {venue_address}. "
        f"Category: {category}. Tags: {tags}. "
        f"Time: {time}. Price: {price}"
    )


def build_event_chunks(events: Sequence[Event]) -> list[Chunk]:
    """Project each `Event` into one `Chunk` with `id=str(event.id)`.

    Preserves input order. An event with a missing/empty id trips a
    `ValueError` — a row without an id is a data bug the caller must fix
    upstream rather than have this layer silently drop.
    """

    chunks: list[Chunk] = []
    for event in events:
        if event.id is None:
            raise ValueError("build_event_chunks requires every event to carry an id")
        chunk_id = str(event.id)
        if not chunk_id:
            raise ValueError("build_event_chunks refuses an empty event id")
        chunks.append(Chunk(id=chunk_id, text=event_to_document(event)))
    return chunks


def search_events_rag(
    events: Sequence[Event],
    query: str,
    *,
    rerank: bool = True,
    k: int = 5,
    n_retrieve: int = 20,
) -> list[Event]:
    """RAG over the events domain: hybrid dense+BM25+RRF then optional cross-encoder rerank.

    Returns up to `k` events in ranked order. `rerank=False` skips the
    cross-encoder stage and returns the top-`k` of the RRF-fused ranking
    directly — the seam the eval harness toggles to attribute the score
    delta to the reranker specifically.

    An empty `events` list or a whitespace-only `query` returns `[]`
    without touching the model — the two natural short-circuits that
    guard the model warm-up cost.

    A `HybridRetriever` and (when `rerank`) a `CrossEncoderReranker` are
    built per call. Sentence-transformers keeps a process-level model
    cache, so the second call in the same process pays only the
    re-embedding cost, not another model load. A future ADR can add a
    cache-with-invalidation layer if the corpus grows past the ~120-event
    scale the eval harness assumes.
    """

    if not events or not query.strip():
        return []

    chunks = build_event_chunks(events)
    events_by_id: dict[str, Event] = {
        chunk.id: event for chunk, event in zip(chunks, events, strict=True)
    }

    retriever = HybridRetriever(chunks)
    fused = retriever.search(query, n_retrieve=n_retrieve)

    if rerank:
        chunks_by_id: dict[str, Chunk] = {chunk.id: chunk for chunk in chunks}
        matched_chunks = [chunks_by_id[hit.chunk_id] for hit in fused.hits]
        reranker = CrossEncoderReranker()
        reranked = reranker.rerank(query, matched_chunks, top_k=k)
        ordered_ids = [hit.chunk_id for hit in reranked.hits]
    else:
        ordered_ids = [hit.chunk_id for hit in fused.hits[:k]]

    return [events_by_id[chunk_id] for chunk_id in ordered_ids]
