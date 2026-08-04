"""Metric scorers for the RAG evaluation harness.

Splits the two families into their own modules: `retrieval.py` holds the
five rank-aware retrieval scorers (`hit@k`, `precision@k`, `recall@k`, MRR,
nDCG@k), and `generation.py` holds the three LLM-judged generation scorers
(Faithfulness, Answer Relevance, Context Precision). Each scorer takes a
per-case input, returns a per-case score, and delegates aggregation to the
harness that composes them — no cross-case state lives here.

Per [ADR 0025](../../../../../docs/adr/0025-rag-over-events.md): scorers
are hand-rolled from scratch rather than pulled from Ragas or DeepEval to
keep the runtime dependency graph small, retain control over the prompt
shape (for the LLM-judged metrics), and keep the cache-key contract in our
own hands.
"""
