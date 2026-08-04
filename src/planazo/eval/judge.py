"""LLM-as-judge plumbing for the generation-eval harness.

Owns the narrow `LLMJudge` protocol the generation scorers depend on, the
concrete `OpenCodeJudge` that reuses the Recommender's OpenCode Zen
chat-model factory, and the disk-cache layer under
`var/eval/judge_cache/{metric}/{case_id}/{answer_hash}.json` that lets a
rerun of the harness be free.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): the judge is
hand-rolled rather than delegated to Ragas or DeepEval — the runtime
dependency graph stays small, the cache key stays under our own control,
and the judge reuses the same OpenCode Zen endpoint the Recommender + the
query interpreter already speak to.

The judge is deliberately failure-tolerant: a malformed model response is
retried once with a stricter reprompt, and if it is still not
JSON-parseable the judge returns `JudgeResponse(score=0.0,
rationale="judge_parse_failed: ...")` so the harness always produces a
result row. AGENTS.md rule 4 stays satisfied — the malformed branch is
still a typed outcome, not a silent success.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agentlib.core import BASE_URL, CHEAP

_JUDGE_SYSTEM = (
    "You are a rigorous evaluator. Follow the rubric in the user message and "
    'reply with ONLY a JSON object matching {"score": float in [0.0, 1.0], '
    '"rationale": string}. No prose, no markdown fences.'
)

_STRICTER_REPROMPT = (
    "Your previous reply was not valid JSON. Reply again with ONLY a JSON "
    'object of shape {"score": float in [0.0, 1.0], "rationale": string}. '
    "No prose, no code fences, no leading or trailing text."
)


class JudgeResponse(BaseModel):
    """One judge decision — numeric score plus a bounded rationale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=1000)


JudgeMetric = Literal[
    "faithfulness",
    "answer_relevance",
    "context_precision",
    "context_recall",
]


class JudgeCacheKey(BaseModel):
    """Coordinates that identify one judge decision on disk.

    `answer_hash` is a stable projection of the judged material (answer +
    chunk signature, or query + chunk signature for context-precision). The
    scorer that constructs the key is the sole authority on the hash; the
    judge only reads it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: JudgeMetric
    case_id: str = Field(min_length=1)
    answer_hash: str = Field(min_length=1)


def compute_answer_hash(payloads: Sequence[str]) -> str:
    """Deterministic sha256-hex-16 over a sequence of strings.

    Joined with a NUL separator that cannot occur in the plain-text inputs
    the scorers feed here, so `["a", "bc"]` and `["ab", "c"]` never collide.
    Callers pass a stable list — typically `[answer, chunk_signature]` or
    `[query, chunk_signature]`.
    """
    material = "\0".join(payloads).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


class LLMJudge(Protocol):
    """Narrow judge surface — the generation scorers only need `.judge`.

    Anything (mock or real) that returns a `JudgeResponse` given a prompt
    and a `JudgeCacheKey` satisfies the contract. The `cache_key` argument
    lets a concrete judge implement disk-cache semantics without the
    scorer knowing anything about disk layout.
    """

    def judge(self, prompt: str, *, cache_key: JudgeCacheKey) -> JudgeResponse: ...


def _cache_path(cache_root: Path, key: JudgeCacheKey) -> Path:
    return cache_root / key.metric / key.case_id / f"{key.answer_hash}.json"


def read_cached_response(cache_root: Path, key: JudgeCacheKey) -> JudgeResponse | None:
    """Return the cached `JudgeResponse` if one exists on disk, else `None`.

    The cache file layout is `{cache_root}/{metric}/{case_id}/{answer_hash}.json`
    with the JSON body carrying at least `score` and `rationale`; extra
    metadata (a `prompt_hash`, a `timestamp`) is tolerated.
    """
    path = _cache_path(cache_root, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or "score" not in payload or "rationale" not in payload:
        return None
    try:
        return JudgeResponse(score=float(payload["score"]), rationale=str(payload["rationale"]))
    except (ValidationError, TypeError, ValueError):
        return None


def write_cached_response(
    cache_root: Path,
    key: JudgeCacheKey,
    response: JudgeResponse,
    *,
    prompt_hash: str,
) -> None:
    """Persist `response` under the cache layout with minimal metadata.

    Only the `prompt_hash` and a UTC timestamp travel alongside the score —
    never the raw prompt, so the tree stays small when scaled to many cases.
    """
    path = _cache_path(cache_root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "score": response.score,
        "rationale": response.rationale,
        "prompt_hash": prompt_hash,
        "written_at_utc": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_judge_output(text: str) -> JudgeResponse | None:
    """Try to parse a raw model reply into a `JudgeResponse`; return `None` on failure."""
    stripped = text.strip()
    if not stripped:
        return None
    # Strip a common code-fence wrapper the model might emit despite the
    # instruction; anything else non-JSON falls through to `None`.
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").removeprefix("JSON").strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "score" not in payload or "rationale" not in payload:
        return None
    try:
        score = float(payload["score"])
    except (TypeError, ValueError):
        return None
    rationale = str(payload["rationale"])
    # Clamp defensively: the model occasionally emits 1.2 or -0.1.
    score = max(0.0, min(1.0, score))
    # Truncate to fit `JudgeResponse.rationale` (max_length=1000).
    if len(rationale) > 1000:
        rationale = rationale[:1000]
    try:
        return JudgeResponse(score=score, rationale=rationale)
    except ValidationError:
        return None


class OpenCodeJudge:
    """Concrete `LLMJudge` that reuses the Recommender's OpenCode Zen plumbing.

    - `cache_root` — directory root under which per-metric/per-case JSON
      files live. Created lazily on first cache write.
    - `model_role` — one of the shared role identifiers (`CHEAP` by default,
      surfaced here as the string literal `"cheap"`). Cost discipline: the
      judge does 60-90 calls per full harness run and we do not want to
      pay for a strong-tier model on every one.
    - `enabled` — when `False`, `judge()` refuses to make a live call. Used
      by tests + a dry-run mode of the harness. A cached response is still
      returned; a cache miss with `enabled=False` returns the safe
      fallback.

    Every judge call:

    1. Consults the disk cache — a hit short-circuits the model call.
    2. On a miss (and only if `enabled`), calls the chat model once,
       parses the reply as JSON, and returns a `JudgeResponse`.
    3. On a parse failure, retries once with a stricter reprompt.
    4. If the second call also fails, returns
       `JudgeResponse(score=0.0, rationale="judge_parse_failed: ...")` and
       still caches the fallback so a rerun does not repeat the wasted
       call. AGENTS.md rule 4 — a malformed model reply is a typed
       branch, not a silent success.
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        model_role: str = CHEAP,
        enabled: bool = True,
    ) -> None:
        self._cache_root = cache_root
        self._model_role = model_role
        self._enabled = enabled
        self._chat_model: ChatOpenAI | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _build_chat_model(self) -> ChatOpenAI:
        api_key = os.environ.get("OPENCODE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENCODE_API_KEY is not set. Add it to a .env file at the repo root."
            )
        return ChatOpenAI(
            model=self._model_role,
            api_key=SecretStr(api_key),
            base_url=BASE_URL,
            use_responses_api=True,
        )

    def _invoke(self, prompt: str, *, stricter: bool) -> str:
        if self._chat_model is None:
            self._chat_model = self._build_chat_model()
        system = _JUDGE_SYSTEM + ("\n\n" + _STRICTER_REPROMPT if stricter else "")
        response = self._chat_model.invoke(
            [SystemMessage(content=system), HumanMessage(content=prompt)]
        )
        content = response.content
        if isinstance(content, str):
            return content
        return "".join(
            part if isinstance(part, str) else str(part.get("text", "")) for part in content
        )

    def judge(self, prompt: str, *, cache_key: JudgeCacheKey) -> JudgeResponse:
        cached = read_cached_response(self._cache_root, cache_key)
        if cached is not None:
            return cached

        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

        if not self._enabled:
            fallback = JudgeResponse(
                score=0.0,
                rationale="judge_parse_failed: judge disabled (no live call made)",
            )
            write_cached_response(self._cache_root, cache_key, fallback, prompt_hash=prompt_hash)
            return fallback

        parsed: JudgeResponse | None = None
        error_note = ""
        try:
            raw = self._invoke(prompt, stricter=False)
            parsed = _parse_judge_output(raw)
            if parsed is None:
                error_note = raw[:120].replace("\n", " ")
                raw = self._invoke(prompt, stricter=True)
                parsed = _parse_judge_output(raw)
                if parsed is None:
                    error_note = raw[:120].replace("\n", " ")
        except Exception as exc:
            error_note = f"{type(exc).__name__}: {exc}"[:200]

        if parsed is None:
            parsed = JudgeResponse(
                score=0.0,
                rationale=f"judge_parse_failed: {error_note}"[:1000],
            )

        write_cached_response(self._cache_root, cache_key, parsed, prompt_hash=prompt_hash)
        return parsed


__all__ = [
    "JudgeCacheKey",
    "JudgeMetric",
    "JudgeResponse",
    "LLMJudge",
    "OpenCodeJudge",
    "compute_answer_hash",
    "read_cached_response",
    "write_cached_response",
]
