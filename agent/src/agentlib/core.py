"""Minimal wrapper over the OpenCode Zen API.

Zen is OpenAI-compatible and served on the Responses API endpoint, so this
module is a thin layer over the OpenAI Python SDK: one `call()` that turns a
prompt (or a `messages` history) into a `Result` carrying the text, token
usage, cost, and whether the reply was cut off by the output-token cap.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI
from openai.types.responses import ResponseUsage

load_dotenv(find_dotenv())

BASE_URL = "https://opencode.ai/zen/v1"

MODELS = {"cheap": "gpt-5.4-nano", "strong": "gpt-5.4"}
CHEAP = MODELS["cheap"]
STRONG = MODELS["strong"]

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    # Constructed lazily so importing this module never requires the key —
    # only making an actual call does.
    global _client
    if _client is None:
        api_key = os.environ.get("OPENCODE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENCODE_API_KEY is not set. Add it to a .env file at the repo root."
            )
        _client = OpenAI(api_key=api_key, base_url=BASE_URL)
    return _client


def __getattr__(name: str) -> OpenAI:
    if name == "client":
        return _get_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# model id -> (input, cached input, output) USD per 1M tokens.
PRICES: dict[str, tuple[float, float, float]] = {
    CHEAP: (0.20, 0.02, 1.25),
    STRONG: (2.50, 0.25, 15.00),
}


def cost(usage: ResponseUsage, model: str) -> float:
    """Price a call's token usage.

    `usage.input_tokens` already includes cached tokens, billed at the
    cheaper cached rate; `usage.output_tokens` already includes reasoning
    tokens, billed at the normal output rate.
    """
    in_rate, cached_rate, out_rate = PRICES[model]
    cached = usage.input_tokens_details.cached_tokens
    non_cached = usage.input_tokens - cached
    return (
        non_cached * in_rate + cached * cached_rate + usage.output_tokens * out_rate
    ) / 1_000_000


@dataclass(frozen=True)
class Result:
    """The outcome of one `call()`."""

    text: str
    model: str
    status: str
    stop_reason: str | None
    truncated: bool  # "it returned text" != "it finished" — True iff cut off by max_output_tokens
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    reasoning_summary: str | None
    # Any: arbitrary JSON-decoded tool-call arguments, shaped by whatever schema the
    # caller registered — {name, arguments, call_id}, no fixed type here.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Any: raw Responses-API output items (message/function_call/reasoning/...), kept
    # as-is so they're re-appendable verbatim to the next turn's `messages`.
    output_items: list[dict[str, Any]] = field(default_factory=list)


def _build_input(
    prompt: str | None,
    messages: list[dict[str, Any]] | None,  # Any: caller-supplied Responses-API message dicts
    system: str | None,
) -> list[dict[str, Any]]:
    if prompt is not None and messages is not None:
        raise ValueError("call() takes either `prompt` or `messages`, not both")
    if messages is not None:
        return list(messages)
    if prompt is None:
        raise ValueError("call() requires either `prompt` or `messages`")
    turns: list[dict[str, Any]] = []
    if system is not None:
        turns.append({"role": "system", "content": system})
    turns.append({"role": "user", "content": prompt})
    return turns


def call(
    prompt: str | None = None,
    *,
    messages: list[dict[str, Any]] | None = None,  # Any: caller-supplied message dicts
    model: str,
    system: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int = 2048,
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    text_format: dict[str, Any] | None = None,  # Any: a caller-supplied JSON Schema document
    tools: list[dict[str, Any]] | None = None,  # Any: caller-supplied tool schema dicts
) -> Result:
    """One Responses-API call, wrapped into a `Result`.

    `temperature` is only sent when the caller passes one explicitly: a
    reasoning call (`reasoning_effort` set to anything but `None`/`"none"`)
    only accepts the API's default temperature and 400s on any other value.
    """
    input_messages = _build_input(prompt, messages, system)

    kwargs: dict[str, Any] = {  # Any: mixed-type Responses-API request kwargs
        "model": model,
        "input": input_messages,
        "max_output_tokens": max_output_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if reasoning_effort not in (None, "none"):
        reasoning: dict[str, Any] = {"effort": reasoning_effort}  # Any: same request-kwargs shape
        if reasoning_summary is not None:
            reasoning["summary"] = reasoning_summary
        kwargs["reasoning"] = reasoning
    if text_format is not None:
        kwargs["text"] = {"format": text_format}
    if tools is not None:
        kwargs["tools"] = tools

    response = _get_client().responses.create(**kwargs)

    tool_calls: list[dict[str, Any]] = []  # Any: see Result.tool_calls
    output_items: list[dict[str, Any]] = []  # Any: see Result.output_items
    summary_parts: list[str] = []
    for item in response.output:
        item_dict = item.model_dump()
        output_items.append(item_dict)
        if item_dict.get("type") == "function_call":
            tool_calls.append(
                {
                    "name": item_dict["name"],
                    "arguments": json.loads(item_dict["arguments"]),
                    "call_id": item_dict["call_id"],
                }
            )
        elif item_dict.get("type") == "reasoning":
            summary_parts.extend(seg["text"] for seg in item_dict.get("summary", []))

    stop_reason = response.incomplete_details.reason if response.incomplete_details else None

    usage = response.usage
    if usage is not None:
        input_tokens = usage.input_tokens
        cached_tokens = usage.input_tokens_details.cached_tokens
        output_tokens = usage.output_tokens
        reasoning_tokens = usage.output_tokens_details.reasoning_tokens
        cost_usd = cost(usage, model)
    else:
        input_tokens = cached_tokens = output_tokens = reasoning_tokens = 0
        cost_usd = 0.0

    return Result(
        text=response.output_text,
        model=model,
        status=response.status or "",
        stop_reason=stop_reason,
        truncated=stop_reason == "max_output_tokens",
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
        reasoning_summary="\n\n".join(summary_parts) or None,
        tool_calls=tool_calls,
        output_items=output_items,
    )


def show(result: Result, label: str) -> None:
    """One-line diagnostic: model, token usage, cost, and stop status."""
    print(
        f"[{label}] {result.model} | in={result.input_tokens} (cached {result.cached_tokens}) "
        f"out={result.output_tokens} (reasoning {result.reasoning_tokens}) | "
        f"${result.cost_usd:.6f} ({result.cost_usd * 100:.4f}¢) | status={result.status}"
    )


def think_aloud(
    prompt: str,
    *,
    model: str = STRONG,
    max_output_tokens: int = 4000,
    effort: str = "high",
    max_retries: int = 5,
) -> Result:
    """`call()` at high reasoning effort, retried until a non-empty reasoning
    summary lands (the API returns one only ~80% of the time on this tier)."""
    result = call(
        prompt,
        model=model,
        reasoning_effort=effort,
        reasoning_summary="auto",
        max_output_tokens=max_output_tokens,
    )
    for _ in range(max_retries - 1):
        if result.reasoning_summary:
            break
        result = call(
            prompt,
            model=model,
            reasoning_effort=effort,
            reasoning_summary="auto",
            max_output_tokens=max_output_tokens,
        )
    return result
