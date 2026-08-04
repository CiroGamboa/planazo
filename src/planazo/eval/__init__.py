"""Evaluation harness for the RAG-backed `search_events` tool.

Owns the golden-dataset loader, the seed-events loader, the LLM-as-judge
plumbing (with disk cache under `var/eval/judge_cache/`), the retrieval
metric scorers (`hit@k`, `precision@k`, `recall@k`, MRR, nDCG@k), and the
generation metric scorers (Faithfulness, Answer Relevance, Context
Precision). The `scripts/run_retrieval_eval.py` and
`scripts/run_generation_eval.py` entry points compose these primitives into
the two harnesses whose outputs land under `data/eval/results/`.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): metrics are
hand-rolled rather than delegated to Ragas or DeepEval so the runtime
dependency graph stays small and the judge cache key stays under our own
control; the judge model reuses `_build_recommender_chat_model` so the
existing OpenCode Zen plumbing carries the eval calls too.
"""
