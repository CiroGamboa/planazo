"""Hand-rolled generation scorers — Faithfulness, Answer Relevance, Context Precision.

Each scorer renders a Markdown prompt template with the per-case
variables, computes a stable cache key over the judged material, and
delegates the numeric decision to an `LLMJudge`. Empty inputs
short-circuit to `JudgeResponse(score=0.0, rationale="...")` without
touching the model — an empty answer cannot be grounded and empty chunks
cannot be precise.

Per [ADR 0025](../../../../../docs/adr/0025-rag-over-events.md): the
generation metrics are hand-rolled rather than delegated to Ragas or
DeepEval — the prompt design, the cache key, and the empty-input semantics
stay under our own control. Judge bias mitigations (temperature 0,
single-role prompts, bounded answer + chunk text, no revealed retrieval
config) live in the prompt templates and the harness that composes these
scorers.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from planazo.eval.judge import (
    JudgeCacheKey,
    JudgeResponse,
    LLMJudge,
    compute_answer_hash,
)

# Bounds defuse verbosity + long-context bias: a rambling answer or an
# oversize chunk should not sway the judge simply by taking more tokens.
_MAX_ANSWER_CHARS: Final[int] = 2000
_MAX_CHUNK_CHARS: Final[int] = 1500

_PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(template_name: str) -> str:
    """Read one of the committed `eval/prompts/*.md` templates as a string."""
    return (_PROMPTS_DIR / template_name).read_text(encoding="utf-8")


def _render(template: str, variables: dict[str, str]) -> str:
    """Substitute every `{{ name }}` placeholder in `template` with `variables[name]`.

    Plain `str.replace` — safer than `str.format` against any `{` character
    that might appear inside a chunk or an answer.
    """
    rendered = template
    for name, value in variables.items():
        rendered = rendered.replace("{{ " + name + " }}", value)
    return rendered


def _format_chunks(chunks: Sequence[str]) -> str:
    """Render a numbered chunk block for the judge prompt.

    Each chunk is trimmed to `_MAX_CHUNK_CHARS` and prefixed with a 1-based
    index so the judge can reason about the set as a whole.
    """
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        trimmed = chunk[:_MAX_CHUNK_CHARS]
        lines.append(f"[{index}] {trimmed}")
    return "\n".join(lines) if lines else "(no chunks were retrieved)"


def _chunk_signature(chunks: Sequence[str]) -> str:
    """Stable signature over the chunk set for cache-key hashing.

    Order-sensitive — a rerank-on and rerank-off run on the same case will
    typically produce different signatures, which is what we want.
    """
    return "\n---\n".join(chunk[:_MAX_CHUNK_CHARS] for chunk in chunks)


def score_faithfulness(
    *,
    answer: str,
    chunks: Sequence[str],
    judge: LLMJudge,
    case_id: str,
) -> JudgeResponse:
    """Score how faithful `answer` is to `chunks` on a `[0.0, 1.0]` scale."""
    bounded_answer = answer[:_MAX_ANSWER_CHARS]
    prompt = _render(
        _load_prompt("faithfulness.md"),
        {"answer": bounded_answer, "chunks": _format_chunks(chunks)},
    )
    cache_key = JudgeCacheKey(
        metric="faithfulness",
        case_id=case_id,
        answer_hash=compute_answer_hash([bounded_answer, _chunk_signature(chunks)]),
    )
    return judge.judge(prompt, cache_key=cache_key)


def score_answer_relevance(
    *,
    query: str,
    answer: str,
    judge: LLMJudge,
    case_id: str,
) -> JudgeResponse:
    """Score how directly `answer` addresses `query` on a `[0.0, 1.0]` scale."""
    bounded_answer = answer[:_MAX_ANSWER_CHARS]
    prompt = _render(
        _load_prompt("answer_relevance.md"),
        {"query": query, "answer": bounded_answer},
    )
    cache_key = JudgeCacheKey(
        metric="answer_relevance",
        case_id=case_id,
        answer_hash=compute_answer_hash([query, bounded_answer]),
    )
    return judge.judge(prompt, cache_key=cache_key)


def score_context_precision(
    *,
    query: str,
    chunks: Sequence[str],
    judge: LLMJudge,
    case_id: str,
) -> JudgeResponse:
    """Score what fraction of `chunks` are relevant to `query` on a `[0.0, 1.0]` scale."""
    prompt = _render(
        _load_prompt("context_precision.md"),
        {"query": query, "chunks": _format_chunks(chunks)},
    )
    cache_key = JudgeCacheKey(
        metric="context_precision",
        case_id=case_id,
        answer_hash=compute_answer_hash([query, _chunk_signature(chunks)]),
    )
    return judge.judge(prompt, cache_key=cache_key)


__all__ = [
    "score_answer_relevance",
    "score_context_precision",
    "score_faithfulness",
]
