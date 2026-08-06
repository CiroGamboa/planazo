# ADR 0025 — RAG over the events catalog

**Status:** Accepted
**Date:** 2026-08-04
**Deciders:** Planazo team
**Related:** [ADR 0003](0003-sqlite-domain-store.md), [ADR 0004](0004-three-store-memory-model.md), [ADR 0008](0008-domain-driven-module-layout.md), [ADR 0014](0014-deterministic-ranking-boundary.md), [ADR 0021](0021-recommender-tool-boundary-shrink.md), [ADR 0023](0023-langgraph-recommender-runtime.md).

## Context

The Recommender's `search_events` tool (registered inline in `planazo.agents.event_agent.run_once`) is today a rigid SQL filter over the `events` table. It takes flat scalars — `category`, `city`, `start_utc_lower`/`start_utc_upper`, `price_max`, `tags` — and applies them as an AND. If a user asks "cheap fun places tonight" and no row carries `category="fun"` + a `price="cheap"` tag, the query returns empty even when a semantically perfect event exists in the free-text `title`, `description`, or `venue_name`. The catalog's richest signal — the natural-language description of what an event actually *is* — is invisible to search.

The Agentic AI Systems HW3 assignment asks for (1) a retrieval layer the agent exposes as an LLM-chosen tool and (2) an evaluation harness that measures the retrieval layer with five rank-aware retrieval metrics plus three generation metrics via LLM-as-judge. Rather than bolt a second retrieval tool alongside `search_events`, the simplest coherent change is to keep the single tool name the Recommender already exposes and give it a RAG-backed body: hard filters still gate the candidate set, and — when a natural-language `query` is present — a hybrid retriever ranks within it.

Events are the natural corpus. Each event row is a short, self-contained record: title (~40 chars), description (~150 chars), venue, tags, category, neighborhood, price, time — every field the retriever needs to score is already on the row. No web sources, no external documents, no chunk-anchor drift.

## Decision

`search_events` becomes RAG-backed. The tool keeps its name and its registration point in `planazo.agents.event_agent.run_once`. Its signature gains one optional field, `query: str | None = None`; hard filters (`category`, `city`, date window, `price_max`) still gate the candidate set, and when `query` is present a hybrid retriever ranks within that set. Callers that omit `query` see today's behavior unchanged — the extension is backward compatible.

**Chunking.** One chunk per event; overlap zero. The event row is the atomic semantic unit; splitting a 150-character description destroys the co-scored bundle (title + venue + tags + neighborhood) the retriever needs. Chunk IDs are event IDs — stable by construction, no content-anchor resolver. The document projection is a deterministic string:

```
"{title}. {description}. Venue: {venue_name} at {venue_address}. Neighborhood: {neighborhood or 'Barcelona'}. Category: {category}. Tags: {tags}. Time: {start_utc}. Price: {price}"
```

**Retrieval + fusion.** Hybrid dense + BM25 with Reciprocal Rank Fusion. Each retriever returns its top-20; the two ranked lists are fused with RRF at `k_rrf = 60` (Cormack, Clarke, Büttcher 2009 default); the fused top-20 is reranked down to top-5. The 15-chunk gap between retrieve depth and return depth is where reranking earns its cost — a chunk RRF ranks #12 can climb to top-3 after cross-encoder scoring, and that reordering is exactly what the retrieval-metrics report needs to show.

**Models.** Local sentence-transformers only. Dense embeddings use `sentence-transformers/all-MiniLM-L6-v2` (384-dimensional). Cross-encoder reranking uses `sentence-transformers/cross-encoder/ms-marco-MiniLM-L-6-v2`. No API key is required for retrieval. First run downloads ~160 MB of model weights into the user-global HuggingFace cache at `~/.cache/huggingface/`; subsequent runs are offline.

**Rerank seam.** `rerank: bool` threads through the retrieval layer so the evaluation harness can run identical queries with rerank on and off and attribute the score delta to the reranker specifically.

**Judge for the generation metrics.** Hand-rolled LLM-as-judge that reuses `_build_recommender_chat_model` (see `planazo.agents.event_agent`), with responses disk-cached under `var/eval/judge_cache/` keyed by metric + case id + answer hash. Rationale: (a) avoids taking on Ragas or DeepEval as a runtime dep for a single-use eval, (b) reuses the existing OpenCode Zen endpoint plumbing Planazo already stands on, (c) gives us full control over the prompt shape, the cache key, and the reproducibility story.

**Corpus growth.** The current `events` table has 27 rows, most demo-short. The five retrieval metrics on 27 documents cannot differentiate BM25 / RRF / rerank — `hit@5` sits near 1.0 for any reasonable retriever. The eval harness therefore loads a committed seed of ~120 LLM-generated realistic Barcelona events (`data/eval/events_seed.jsonl`) alongside the exact generation prompt (`data/eval/generation_prompt.md`) so the run is reproducible. Production `events` is untouched.

## Consequences

- The Recommender's tool boundary stays unchanged in name and count: still one `search_events`, still registered where it was. Backward compatibility for hard-filter-only callers is preserved.
- The repository gains four runtime dependencies: `sentence-transformers` (pulling `torch` + `transformers` as transitive weight — first-run ~160 MB download), `rank-bm25`, `numpy`, and `tiktoken`.
- A new `data/eval/` corpus and a new `var/eval/judge_cache/` runtime cache land in the tree. The judge cache is gitignored; the seed events + golden dataset + generation prompt + committed results are tracked.
- Retrieval runs in-process per Recommender turn. The RAG index rebuilds each turn from the current `events` snapshot; at ~120 rows this is cheap (~50 ms embed + BM25 fit). A future ADR can add a cache-with-invalidation layer if the corpus grows.
- The eval harness is a committed, deterministic tool the report can point at — `scripts/run_retrieval_eval.py` and `scripts/run_generation_eval.py` — so reviewers can rerun the numbers.

## Rejected alternatives

1. **Web-source RAG (Tavily, Serper, or similar).** Deferred. Introduces an API-key dependency, a snapshot-versioning story, and a whole new source-integration surface — all orthogonal to the HW3 requirement of demonstrating a retrieval layer over Planazo's own domain data. The events catalog is a real corpus; there is no need to invent one.
2. **Persistent vector index (Chroma, FAISS, LanceDB).** Deferred. At ~120 events, an in-memory numpy cosine-similarity pass is faster than any client hop and adds no runtime dependency. When the catalog crosses a threshold where index-build latency starts to hurt (~5k+ events), a future ADR can add persistence with proper invalidation semantics.
3. **Ragas or DeepEval for generation metrics.** Rejected. Both drag in bulky dependency trees for what is a single-use evaluation, and both push us toward their prompt shapes and their cache keys. Hand-rolled scorers reuse `_build_recommender_chat_model`, give us full control over the prompts, and keep the runtime dependency graph small.
4. **Splitting long event descriptions across multiple chunks.** Rejected. Event descriptions in the catalog are short (~150 chars) and self-contained; the retriever wants to score title + description + venue + tags together, not shard them. Chunk-per-event keeps the atomic semantic unit intact and makes chunk IDs identical to stable event IDs — no anchor-resolver needed.

## Follow-up work

- Implement the retrieval primitives (`rag/retrieval.py`, `rag/rerank.py`, `rag/models.py`) and the domain glue in `catalog/rag.py`.
- Extend `catalog.tools.search_events` with the `query` field and update the inline registration in `event_agent.run_once` to pass it through.
- Author the golden dataset at `data/eval/questions.jsonl` (≥20 cases, ≥5 failure categories, ≥1 out-of-corpus).
- Generate and commit the ~120-event seed with the exact prompt used.
- Run both eval harnesses and commit results + judge cache; document the retrieval table, the generation table, and the disagreement analysis in the README's HW3 section.
- Flip this ADR to `Accepted` once the tool is wired and the integration test passes.
