"""LangChain wire-shape lock for the Extractor's multimodal injection.

The Extractor's ``post_tools`` node emits a ``HumanMessage`` whose content is a
Responses-API-shaped ``list[dict]`` with ``input_text`` + ``input_image``
parts. That message is then sent to ``ChatOpenAI(use_responses_api=True)``, so
the LangChain conversion path must preserve those parts unchanged. This test
constructs the exact shape the injection node produces and asserts the pinned
LangChain's request-payload conversion returns ``input_text`` and
``input_image`` parts with URL and type strings intact — a future minor bump
that quietly rewraps ``input_image`` → ``image_url`` fails this test loudly.

Runs under the default ``uv run pytest`` — no live LLM roundtrip, no network.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def _extract_parts(payload: Any) -> list[dict[str, Any]]:
    """Return the content parts LangChain emitted, regardless of wrapper shape.

    The Responses-API payload is a nested structure; the request body carries
    the injected content parts under the ``input`` array. Walk the object and
    collect every dict that looks like a Responses-API content part
    (``{"type": "input_text" | "input_image", ...}``).
    """

    collected: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if isinstance(node_type, str) and node_type.startswith("input_"):
                collected.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return collected


def test_multimodal_human_message_passes_through_responses_api_input() -> None:
    """``HumanMessage`` with Responses-API content parts survives conversion.

    Constructs the exact ``content`` shape the Extractor's injection node
    emits (``[{"type": "input_text", ...}, {"type": "input_image", ...}]``),
    feeds it through the currently pinned LangChain's ``_get_request_payload``
    on a stub ``ChatOpenAI(use_responses_api=True)`` client, and asserts both
    parts appear unchanged in the emitted request payload.
    """

    image_url = "https://example.invalid/x.jpg"
    prefix_text = (
        "Image content from the fetched post — https://example.invalid/p/ABC/ (kind=image):"
    )
    human_message = HumanMessage(
        content=[
            {"type": "input_text", "text": prefix_text},
            {"type": "input_image", "image_url": image_url},
        ]
    )

    chat_model = ChatOpenAI(
        model="stub",
        api_key=SecretStr("stub"),
        base_url="http://stub.invalid",
        use_responses_api=True,
    )

    payload = chat_model._get_request_payload([human_message])
    parts = _extract_parts(payload)

    # Prove the two parts survived — text and image both present.
    text_parts = [part for part in parts if part.get("type") == "input_text"]
    image_parts = [part for part in parts if part.get("type") == "input_image"]

    assert any(part.get("text") == prefix_text for part in text_parts), (
        f"input_text part missing or altered; payload={json.dumps(payload, default=str)}"
    )
    assert any(part.get("image_url") == image_url for part in image_parts), (
        f"input_image part missing or altered; payload={json.dumps(payload, default=str)}"
    )
