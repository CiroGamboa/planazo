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
