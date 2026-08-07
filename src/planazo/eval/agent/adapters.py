"""Trace-to-scorer-input adapters (HW4 Part 2 wiring).

Three pure functions of an ``mlflow.entities.Trace``:

- ``trace_to_tool_calls`` — walks the ``TOOL`` spans, sorts by start
  time, and materialises each into a ``ToolCall`` the trajectory metrics
  consume. LangGraph's ``ToolNode`` autolog stores the tool's arguments
  under ``mlflow.spanInputs``.
- ``trace_to_retrieval_inputs`` — finds the ``RETRIEVER`` span from the
  ``@mlflow.trace`` on ``catalog.rag.search_events_rag`` and pulls the
  ranked event ids from its output. Returns ``None`` when no retrieval
  span exists (e.g. a chat-only trace or a search with an empty query).
- ``trace_to_generation_inputs`` — returns the ``(query, answer,
  chunks)`` tuple the HW3 generation scorers consume. ``query`` is
  drawn from the root ``AGENT`` span's input; ``answer`` from the final
  ``CHAT_MODEL`` span's output; ``chunks`` from the ``RETRIEVER`` span's
  output projected through ``event_to_document``. Returns ``None`` when
  any piece is missing.

All three adapters read only span metadata — they never call the model
or open the catalog. Scorer bodies stay frozen (AGENTS.md rule 7 + the
HW4 assignment); adapters ferry data into their existing signatures.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mlflow.entities import Span, Trace

from planazo.catalog.models import Event
from planazo.catalog.rag import event_to_document
from planazo.eval.agent.models import ToolCall

_SPAN_INPUTS_KEY = "mlflow.spanInputs"
_SPAN_OUTPUTS_KEY = "mlflow.spanOutputs"


def _spans_by_type(trace: Trace, span_type: str) -> list[Span]:
    """Return every span on ``trace`` whose ``span_type`` matches, in trace order."""
    return [span for span in trace.data.spans if span.span_type == span_type]


def _get_inputs(span: Span) -> dict[str, Any] | None:
    """Read ``mlflow.spanInputs`` as a dict, or ``None`` when absent/wrong shape."""
    value = span.attributes.get(_SPAN_INPUTS_KEY)
    if isinstance(value, dict):
        return value
    return None


def _get_outputs(span: Span) -> Any:
    """Read ``mlflow.spanOutputs`` verbatim (list, dict, str, or ``None``)."""
    return span.attributes.get(_SPAN_OUTPUTS_KEY)


def trace_to_tool_calls(trace: Trace) -> list[ToolCall]:
    """Materialise the trace's ``TOOL`` spans into ordered ``ToolCall`` rows.

    The tool span's ``name`` is the tool identifier LangGraph registered
    it under (``search_events``, ``retrieve_memory``, etc.). Arguments are
    read from ``mlflow.spanInputs`` — a missing or non-dict value is
    treated as an empty argument dict, not a hard error, so a trace with
    a partially-serialised span still produces a scorable trajectory.
    Spans are ordered by ``start_time_ns`` so the trajectory metrics see
    the model's actual call order.
    """
    tool_spans = _spans_by_type(trace, "TOOL")
    tool_spans.sort(key=lambda span: span.start_time_ns)
    calls: list[ToolCall] = []
    for span in tool_spans:
        arguments = _get_inputs(span) or {}
        calls.append(ToolCall(tool=span.name, arguments=dict(arguments)))
    return calls


def trace_to_retrieval_inputs(trace: Trace) -> list[str] | None:
    """Return the ranked event-id list from the first ``RETRIEVER`` span.

    ``catalog.rag.search_events_rag`` is decorated ``span_type=RETRIEVER``
    and returns a list of ``Event``-shaped dicts. This adapter reads that
    list and projects each entry to ``str(event["id"])`` so the retrieval
    scorers receive the exact ``Chunk.id`` shape they expect. Returns
    ``None`` when the trace has no retriever span or when the span's
    output is not the expected list shape.
    """
    retriever_spans = _spans_by_type(trace, "RETRIEVER")
    if not retriever_spans:
        return None
    retriever_spans.sort(key=lambda span: span.start_time_ns)
    output = _get_outputs(retriever_spans[0])
    if not isinstance(output, list):
        return None
    ranked_ids: list[str] = []
    for event in output:
        if isinstance(event, dict) and "id" in event and event["id"] is not None:
            ranked_ids.append(str(event["id"]))
    return ranked_ids


def _root_agent_span(trace: Trace) -> Span | None:
    """Return the first ``AGENT`` span in trace order, or ``None`` if none exists."""
    agent_spans = _spans_by_type(trace, "AGENT")
    if not agent_spans:
        return None
    agent_spans.sort(key=lambda span: span.start_time_ns)
    return agent_spans[0]


def _final_chat_answer(trace: Trace) -> str | None:
    """Return the final ``CHAT_MODEL`` span's answer text, or ``None`` if absent.

    LangChain's autolog stores the LLM output under ``mlflow.spanOutputs``
    as a ``LLMResult``-shaped dict with a ``generations`` field. This
    reader digs one level in to recover the text of the final generation,
    then falls back to the root ``AGENT`` span's answer field when the
    chat-model shape drifts.
    """
    chat_spans = _spans_by_type(trace, "CHAT_MODEL")
    if not chat_spans:
        return None
    chat_spans.sort(key=lambda span: span.start_time_ns)
    final = chat_spans[-1]
    output = _get_outputs(final)
    if not isinstance(output, dict):
        return None
    generations = output.get("generations")
    if not isinstance(generations, list) or not generations:
        return None
    first_batch = generations[0]
    if not isinstance(first_batch, list) or not first_batch:
        return None
    generation = first_batch[-1]
    if not isinstance(generation, dict):
        return None
    text = generation.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def _agent_answer_from_root(root: Span) -> str | None:
    """Fallback answer extraction from the root ``AGENT`` span's output."""
    output = _get_outputs(root)
    if not isinstance(output, dict):
        return None
    answer = output.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer
    return None


def _agent_query_from_root(root: Span) -> str | None:
    """Recover the user's query from the root ``AGENT`` span's input.

    ``recommender.run_once`` is called with the validated ``SearchIntent``
    plus ``run_context`` — the raw user text (when supplied) lives at
    ``run_context.text``. Falls back to a JSON rendering of the intent
    when the raw text is absent, so the retrieval scorers always receive
    a non-empty query string.
    """
    inputs = _get_inputs(root)
    if not isinstance(inputs, dict):
        return None
    run_context = inputs.get("run_context")
    if isinstance(run_context, dict):
        text = run_context.get("text")
        if isinstance(text, str) and text.strip():
            return text
    intent = inputs.get("intent")
    if isinstance(intent, dict):
        return str(intent)
    return None


def _chunks_from_retrieval(trace: Trace) -> list[str] | None:
    """Project the retrieval span's ranked events into HW3 chunk documents.

    Each event dict is passed through ``event_to_document`` — the same
    projection ``catalog.rag`` uses to build the retriever's chunk texts.
    Rows that fail ``Event`` validation are skipped (the ranked-id list
    was already the source of truth for the retrieval scorer; the
    generation scorer only needs textual chunks). Returns ``None`` when
    no retriever span exists on the trace.
    """
    retriever_spans = _spans_by_type(trace, "RETRIEVER")
    if not retriever_spans:
        return None
    retriever_spans.sort(key=lambda span: span.start_time_ns)
    output = _get_outputs(retriever_spans[0])
    if not isinstance(output, list):
        return None
    chunks: list[str] = []
    for raw in output:
        if not isinstance(raw, dict):
            continue
        try:
            event = Event.model_validate(raw)
        except Exception:
            continue
        chunks.append(event_to_document(event))
    return chunks


def trace_to_generation_inputs(trace: Trace) -> tuple[str, str, list[str]] | None:
    """Return ``(query, answer, chunks)`` for the generation scorers.

    Every piece must be present: the root ``AGENT`` span for the query,
    a ``CHAT_MODEL`` (or root fallback) for the answer, and a
    ``RETRIEVER`` span for the chunks. When any is missing the adapter
    returns ``None`` so the batch scorer runner skips this trace rather
    than send an empty prompt to the judge.
    """
    root = _root_agent_span(trace)
    if root is None:
        return None
    query = _agent_query_from_root(root)
    if query is None or not query.strip():
        return None
    answer = _final_chat_answer(trace) or _agent_answer_from_root(root)
    if answer is None or not answer.strip():
        return None
    chunks = _chunks_from_retrieval(trace)
    if chunks is None:
        return None
    return query, answer, chunks


def _cast_sequence_of_str(values: Sequence[str]) -> list[str]:
    """Narrow-copy helper used by the tests to convert tuple ↔ list at boundaries."""
    return list(values)


__all__ = [
    "trace_to_generation_inputs",
    "trace_to_retrieval_inputs",
    "trace_to_tool_calls",
]
