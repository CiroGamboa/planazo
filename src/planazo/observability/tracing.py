"""MLflow tracing configuration for Planazo (HW4).

Wires `mlflow.langchain.autolog()` at composition roots so every Recommender
(and, when configured, Extractor + Curator) invocation emits a span tree with
LLM calls, tool calls, and retrieval steps — the exact shape HW4 Part 2 asks
for. Two hand-rolled `@mlflow.trace` decorators live outside this module:

- `agents.event_agent.run_once` — root span per invocation.
- `catalog.rag.search_events_rag` — RETRIEVAL span whose output includes the
  ranked event ids the retrieval scorers consume.

Autolog covers LLM + TOOL spans through the LangGraph runtime.

Design:

- `configure_tracing()` is idempotent — safe to call from every composition
  root; a second call is a no-op.
- `MLFLOW_TRACKING_URI` defaults to `file:./var/mlflow` (bind-mounted under
  the Docker `var/` volume from ADR 0026), so `docker compose up` requires
  no additional wiring.
- Tags are set through thin helpers so composition roots do not import
  `mlflow` directly and callers see one obvious way to tag an invocation.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

import logging
import os
from typing import Final, Literal

import mlflow

_DEFAULT_TRACKING_URI: Final[str] = "file:./var/mlflow"
_DEFAULT_EXPERIMENT: Final[str] = "planazo"

_TAG_REQUEST_ORIGIN: Final[str] = "request_origin"
_TAG_EVAL_CASE_ID: Final[str] = "eval_case_id"
_TAG_AGENT_KIND: Final[str] = "agent_kind"

RequestOrigin = Literal["bot", "cli", "eval", "batch"]
AgentKind = Literal["recommender", "curator", "extractor"]

_configured: bool = False
_log = logging.getLogger(__name__)


def configure_tracing(experiment: str = _DEFAULT_EXPERIMENT) -> None:
    """Configure MLflow tracking + LangChain autolog once per process.

    Reads `MLFLOW_TRACKING_URI` from the environment; falls back to a local
    file store under `var/mlflow/`. Subsequent calls are no-ops so every
    composition root can call this without coordinating.

    If MLflow itself errors during setup (offline tracking server,
    permission denied), we log one line and continue — tracing must never
    take down the primary flow (AGENTS.md rule 4).
    """
    global _configured
    if _configured:
        return
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI)
    try:
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment)
        mlflow.langchain.autolog()
    except Exception as exc:  # pragma: no cover - degraded-only branch
        _log.warning("mlflow tracing setup failed: %s", exc)
        return
    _configured = True


def set_request_origin(origin: RequestOrigin) -> None:
    """Tag the active trace with the invocation surface.

    `bot` / `cli` / `eval` / `batch` — a small, bounded enum, safe to filter
    on in the MLflow UI. Session-12's high-cardinality-tag warning does not
    apply.
    """
    _safe_update_tag(_TAG_REQUEST_ORIGIN, origin)


def set_eval_case_id(case_id: str) -> None:
    """Tag the active trace with the eval case id (join key back to scenarios).

    Cardinality is bounded by the eval scenario file (12 today), so this is
    a safe tag even though the value is more descriptive than an enum.
    """
    _safe_update_tag(_TAG_EVAL_CASE_ID, case_id)


def set_agent_kind(kind: AgentKind) -> None:
    """Tag the active trace with which Planazo agent produced it.

    Separates Recommender / Curator / Extractor traces in a single MLflow
    experiment without needing three experiments.
    """
    _safe_update_tag(_TAG_AGENT_KIND, kind)


def _safe_update_tag(key: str, value: str) -> None:
    """Set a tag on the active trace; swallow errors so tracing never breaks callers."""
    try:
        mlflow.update_current_trace(tags={key: value})
    except Exception as exc:  # pragma: no cover - degraded-only branch
        _log.debug("mlflow tag update failed for %s=%s: %s", key, value, exc)


def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """Approximate the token count of `text` using tiktoken.

    The Recommender's OpenCode Zen backend does not return a token-usage
    field in the LangChain response, so autolog cannot record token counts
    on the CHAT_MODEL span. HW4 asks token counts to be visible on
    relevant spans; this helper lets `run_once` post a trace-level
    estimate via `mlflow.update_current_trace(metadata=...)` after the
    graph returns. It is an approximation (encoder mismatch is possible),
    not a real usage record — the report calls this out.

    Falls back to a whitespace token count if tiktoken cannot load an
    encoder for `model` (offline scenario, or an unrecognized model id).
    """
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # pragma: no cover - degraded-only branch
        return len(text.split())


def set_token_usage(input_text: str, output_text: str) -> None:
    """Attach approximate token counts as trace tags (tiktoken estimate).

    Compensates for the OpenCode Zen backend not returning `token_usage`
    in the LangChain response shape — see ADR 0027 decision 5. Uses
    trace tags rather than metadata because the mlflow 2.22 file backend
    accepts tags but the `metadata` kwarg on `update_current_trace` is
    signature-only in this version.
    """
    try:
        input_tokens = estimate_tokens(input_text)
        output_tokens = estimate_tokens(output_text)
        mlflow.update_current_trace(
            tags={
                "tokens.input_estimate": str(input_tokens),
                "tokens.output_estimate": str(output_tokens),
                "tokens.total_estimate": str(input_tokens + output_tokens),
                "tokens.source": "tiktoken_estimate",
            }
        )
    except Exception as exc:  # pragma: no cover - degraded-only branch
        _log.debug("mlflow token-usage tag update failed: %s", exc)
