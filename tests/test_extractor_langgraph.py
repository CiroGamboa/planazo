"""Focused contract tests for the Extractor's LangGraph runtime topology."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from planazo.agents.langgraph_runtime import (
    ExtractorGraphInput,
    ExtractorGraphState,
    build_extractor_graph,
    build_langchain_tools,
    invoke_extractor_graph,
)


def _fetch_instagram_post(url: str) -> dict[str, object]:
    """Return a stub Instagram post payload for this deterministic graph test."""

    return {"url": url, "media": {"kind": "image", "url": "https://example/x.jpg"}}


def _save_event(title: str) -> dict[str, object]:
    """Persist a stub event for this deterministic graph test."""

    return {"saved": True, "title": title}


def _report_extraction_status(reason: str) -> dict[str, object]:
    """Report an unhappy terminal status for this deterministic graph test."""

    return {"reason": reason}


class ScriptedExtractorModel:
    """LangChain-compatible fake that plays a pre-scripted list of AIMessages."""

    def __init__(self, script: Sequence[AIMessage]) -> None:
        self._script = list(script)
        self.messages_seen: list[list[BaseMessage]] = []
        self.bound_tools: Sequence[BaseTool] = ()

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        self.bound_tools = tools
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.messages_seen.append(list(messages))
        if not self._script:
            raise AssertionError("scripted model exhausted before graph terminated")
        return self._script.pop(0)


class RepeatedFetchModel:
    """Fake model that always requests ``fetch_instagram_post`` until the cap fires."""

    def __init__(self) -> None:
        self.invocations = 0

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fetch_instagram_post",
                    "args": {"url": "https://example/p/1"},
                    "id": f"call-{self.invocations}",
                }
            ],
        )


def _valid_input(**overrides: object) -> ExtractorGraphInput:
    values: dict[str, object] = {
        "url": "https://example/p/1",
        "delegator_user_id": 42,
        "run_id": "run-1",
        "system_prompt": "Extract the event from the post.",
        "user_message": "Extract this URL.",
    }
    values.update(overrides)
    return ExtractorGraphInput(**values)  # type: ignore[arg-type]


def test_typed_extractor_graph_state_has_no_ad_hoc_mapping_fields() -> None:
    annotations = ExtractorGraphState.__annotations__

    assert set(annotations) == {
        "messages",
        "url",
        "delegator_user_id",
        "run_id",
        "system_prompt",
        "model_steps",
        "max_model_steps",
        "stopped",
    }
    assert all("dict" not in str(annotation).lower() for annotation in annotations.values())


def test_extractor_graph_input_rejects_delegator_user_id_zero() -> None:
    with pytest.raises(ValidationError):
        _valid_input(delegator_user_id=0)


def test_extractor_graph_input_rejects_empty_url() -> None:
    with pytest.raises(ValidationError):
        _valid_input(url="")


def test_extractor_graph_input_rejects_empty_run_id() -> None:
    with pytest.raises(ValidationError):
        _valid_input(run_id="")


def test_extractor_graph_input_rejects_max_model_steps_zero() -> None:
    with pytest.raises(ValidationError):
        _valid_input(max_model_steps=0)


def test_extractor_graph_input_rejects_max_model_steps_sixty_five() -> None:
    with pytest.raises(ValidationError):
        _valid_input(max_model_steps=65)


def test_extractor_graph_config_recursion_limit_at_max_thirty_two() -> None:
    request = _valid_input(max_model_steps=32)

    assert request.graph_config()["recursion_limit"] == 130


def test_extractor_graph_config_recursion_limit_at_max_one() -> None:
    request = _valid_input(max_model_steps=1)

    assert request.graph_config()["recursion_limit"] == 6


def test_extractor_graph_dispatches_fetch_then_save_then_terminates() -> None:
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fetch_instagram_post",
                    "args": {"url": "https://example/p/1"},
                    "id": "call-fetch",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_event",
                    "args": {"title": "Rooftop DJ set"},
                    "id": "call-save",
                }
            ],
        ),
        AIMessage(content=""),
    ]
    model = ScriptedExtractorModel(script)
    tools = build_langchain_tools(
        {
            "fetch_instagram_post": _fetch_instagram_post,
            "save_event": _save_event,
            "report_extraction_status": _report_extraction_status,
        }
    )
    graph = build_extractor_graph(model, tools)

    state = invoke_extractor_graph(graph, _valid_input())

    tool_messages = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    tool_names = [m.name for m in tool_messages]
    assert tool_names == ["fetch_instagram_post", "save_event"]
    assert state["stopped"] == "answered"
    assert state["model_steps"] == 3


def test_extractor_post_tools_hook_runs_once_per_tool_node_execution() -> None:
    visits: list[ExtractorGraphState] = []
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fetch_instagram_post",
                    "args": {"url": "https://example/p/1"},
                    "id": "call-fetch",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_event",
                    "args": {"title": "Rooftop DJ set"},
                    "id": "call-save",
                }
            ],
        ),
        AIMessage(content=""),
    ]
    model = ScriptedExtractorModel(script)
    tools = build_langchain_tools(
        {
            "fetch_instagram_post": _fetch_instagram_post,
            "save_event": _save_event,
            "report_extraction_status": _report_extraction_status,
        }
    )

    def post_tools(state: ExtractorGraphState) -> list[BaseMessage]:
        visits.append(state)
        return [HumanMessage(content=f"post_tools#{len(visits)}")]

    graph = build_extractor_graph(model, tools, post_tools=post_tools)

    state = invoke_extractor_graph(graph, _valid_input())

    assert len(visits) == 2
    # Each visit receives the running state with the delegator id + url set.
    assert all(v["url"] == "https://example/p/1" for v in visits)
    assert all(v["delegator_user_id"] == 42 for v in visits)
    # The injected HumanMessages sit between the ToolMessage and the next AIMessage.
    human_contents = [m.content for m in state["messages"] if isinstance(m, HumanMessage)]
    assert "post_tools#1" in human_contents
    assert "post_tools#2" in human_contents
    # The second agent-turn saw the first injected message before it emitted save_event.
    second_call_texts = [str(m.content) for m in model.messages_seen[1]]
    assert "post_tools#1" in second_call_texts


def test_extractor_graph_reaches_step_cap_at_max_model_steps_one() -> None:
    tools = build_langchain_tools(
        {
            "fetch_instagram_post": _fetch_instagram_post,
            "save_event": _save_event,
            "report_extraction_status": _report_extraction_status,
        }
    )
    graph = build_extractor_graph(RepeatedFetchModel(), tools)
    request = _valid_input(max_model_steps=1)

    state = invoke_extractor_graph(graph, request)

    assert state["model_steps"] == 1
    assert state["stopped"] == "max_steps"
    assert any(isinstance(m, ToolMessage) for m in state["messages"])


def test_graph_reaches_step_cap_at_max_model_steps_thirty_two() -> None:
    tools = build_langchain_tools(
        {
            "fetch_instagram_post": _fetch_instagram_post,
            "save_event": _save_event,
            "report_extraction_status": _report_extraction_status,
        }
    )

    def _noop_post_tools(state: ExtractorGraphState) -> list[BaseMessage]:
        return []

    model = RepeatedFetchModel()
    graph = build_extractor_graph(model, tools, post_tools=_noop_post_tools)
    request = _valid_input(max_model_steps=32)

    state = invoke_extractor_graph(graph, request)

    assert state["stopped"] == "max_steps"
    assert state["model_steps"] == 32


def test_extractor_graph_terminal_detection_ignores_tool_names() -> None:
    # The graph does not short-circuit on save_event or report_extraction_status
    # names — it terminates only when the model emits an AIMessage with no
    # tool_calls. A save_event call followed by another tool_call turn is
    # dispatched normally.
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_event",
                    "args": {"title": "Draft event"},
                    "id": "call-save-1",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "report_extraction_status",
                    "args": {"reason": "low_confidence_extraction"},
                    "id": "call-report",
                }
            ],
        ),
        AIMessage(content="all done"),
    ]
    model = ScriptedExtractorModel(script)
    tools = build_langchain_tools(
        {
            "fetch_instagram_post": _fetch_instagram_post,
            "save_event": _save_event,
            "report_extraction_status": _report_extraction_status,
        }
    )
    graph = build_extractor_graph(model, tools)

    state = invoke_extractor_graph(graph, _valid_input())

    tool_names = [m.name for m in state["messages"] if isinstance(m, ToolMessage)]
    assert tool_names == ["save_event", "report_extraction_status"]
    assert state["stopped"] == "answered"
    assert state["model_steps"] == 3


def test_extractor_graph_rejects_a_docstring_less_tool_callable() -> None:
    def undocumented(url: str) -> dict[str, object]:
        return {"url": url}

    with pytest.raises(ValueError, match="requires a docstring"):
        build_extractor_graph(
            ScriptedExtractorModel([]),
            build_langchain_tools({"fetch_instagram_post": undocumented}),
        )
