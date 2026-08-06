"""Metric scorers for the RAG evaluation harness.

`retrieval.py` holds the five rank-aware retrieval scorers (`hit@k`,
`precision@k`, `recall@k`, MRR, nDCG@k); `generation.py` holds the three
LLM-as-judge scorers (Faithfulness, Answer Relevance, Context Precision).
Each scorer takes a per-case input, returns a per-case score, and
delegates aggregation to the harness that composes them — no cross-case
state lives here.

Per [ADR 0025](../../../../../docs/adr/0025-rag-over-events.md): scorers
are hand-rolled from scratch rather than pulled from Ragas or DeepEval to
keep the runtime dependency graph small, keep the empty-golden semantics
in our own hands, and keep the judge cache key + prompt design under our
own control.
"""

from planazo.eval.judge import (
    JudgeCacheKey,
    JudgeResponse,
    LLMJudge,
    OpenCodeJudge,
)
from planazo.eval.metrics.generation import (
    score_answer_relevance,
    score_context_precision,
    score_faithfulness,
)
from planazo.eval.metrics.retrieval import (
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

__all__ = [
    "JudgeCacheKey",
    "JudgeResponse",
    "LLMJudge",
    "OpenCodeJudge",
    "hit_at_k",
    "mrr",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "score_answer_relevance",
    "score_context_precision",
    "score_faithfulness",
]
