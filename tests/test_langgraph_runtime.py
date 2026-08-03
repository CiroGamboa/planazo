"""Focused contract tests for the Recommender's LangGraph runtime topology."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from planazo.agents.langgraph_runtime import (
    RecommenderGraphInput,
    build_langchain_tools,
    build_recommender_graph,
    invoke_recommender_graph,
    open_recommender_checkpointer,
)
from planazo.query.models import SearchIntent


def _intent() -> SearchIntent:
    start = datetime(2026, 8, 3, 18, tzinfo=UTC)
    return SearchIntent(start_utc=start, end_utc=start + timedelta(hours=4), city="Barcelona")


def _uppercase(text: str) -> str:
    """Return text in uppercase for a deterministic graph-tool test."""

    return text.upper()


class ScriptedToolCallingModel:
    """Small LangChain-compatible fake whose first answer selects a tool."""

    def __init__(self) -> None:
        self.bound_tools: Sequence[BaseTool] = ()
        self.calls = 0

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        self.bound_tools = tools
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.calls += 1
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="The tool result is ready.")
        return AIMessage(
            content="",
            tool_calls=[{"name": "uppercase", "args": {"text": "planazo"}, "id": "call-1"}],
        )


class AnswerOnlyModel:
    """A fake model that ends at the conditional edge without a tool call."""

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return AIMessage(content="No tool is needed.")


class RepeatedToolModel:
    """A fake model that keeps requesting a tool until the graph cap stops it."""

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": "uppercase", "args": {"text": "again"}, "id": "call-repeat"}],
        )


class ResumeAfterToolModel:
    """A recreated model that can finish only when a prior tool result persists."""

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if any(
            isinstance(message, ToolMessage) and message.content == "AGAIN" for message in messages
        ):
            return AIMessage(content="Resumed from the saved tool result.")
        raise AssertionError("the recreated graph did not receive the prior tool output")


def _request() -> RecommenderGraphInput:
    return RecommenderGraphInput(
        user_id=1,
        intent=_intent(),
        system_prompt="Use tools only when needed.",
        user_message="Uppercase Planazo.",
        thread_id="test-user-1",
    )


def test_graph_registers_a_langchain_tool_and_returns_to_the_model_after_a_tool_call() -> None:
    model = ScriptedToolCallingModel()
    tools = build_langchain_tools({"uppercase": _uppercase})
    graph = build_recommender_graph(model, tools)

    state = invoke_recommender_graph(graph, _request())

    assert [tool.name for tool in model.bound_tools] == ["uppercase"]
    assert model.calls == 2
    assert any(
        isinstance(message, ToolMessage) and message.content == "PLANAZO"
        for message in state["messages"]
    )
    assert state["messages"][-1].content == "The tool result is ready."
    assert state["model_steps"] == 2


def test_graph_ends_after_the_agent_node_when_the_model_requests_no_tool() -> None:
    graph = build_recommender_graph(
        AnswerOnlyModel(),
        build_langchain_tools({"uppercase": _uppercase}),
    )

    state = invoke_recommender_graph(graph, _request())

    assert len(state["messages"]) == 2
    assert state["messages"][-1].content == "No tool is needed."
    assert state["model_steps"] == 1


def test_graph_dispatches_the_final_tool_then_stops_at_the_model_step_cap() -> None:
    graph = build_recommender_graph(
        RepeatedToolModel(),
        build_langchain_tools({"uppercase": _uppercase}),
    )
    request = _request().model_copy(update={"max_model_steps": 1})

    state = invoke_recommender_graph(graph, request)

    assert state["model_steps"] == 1
    assert state["stopped"] == "max_steps"
    assert any(isinstance(message, ToolMessage) for message in state["messages"])


def test_sqlite_checkpoint_resumes_a_stopped_tool_turn_after_graph_recreation(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "recommender-checkpoints.sqlite3"
    stopped_request = _request().model_copy(update={"max_model_steps": 1})

    with open_recommender_checkpointer(checkpoint_path) as first_checkpointer:
        first_graph = build_recommender_graph(
            RepeatedToolModel(),
            build_langchain_tools({"uppercase": _uppercase}),
            checkpointer=first_checkpointer,
        )
        stopped_state = invoke_recommender_graph(first_graph, stopped_request)

    assert stopped_state["stopped"] == "max_steps"
    assert checkpoint_path.exists()

    with open_recommender_checkpointer(checkpoint_path) as resumed_checkpointer:
        resumed_graph = build_recommender_graph(
            ResumeAfterToolModel(),
            build_langchain_tools({"uppercase": _uppercase}),
            checkpointer=resumed_checkpointer,
        )
        resumed_state = invoke_recommender_graph(
            resumed_graph,
            _request().model_copy(update={"user_message": "Please finish this turn."}),
        )

    assert resumed_state["stopped"] == "answered"
    assert any(
        isinstance(message, ToolMessage) and message.content == "AGAIN"
        for message in resumed_state["messages"]
    )
    assert resumed_state["messages"][-1].content == "Resumed from the saved tool result."


def test_graph_input_validates_the_application_boundary_before_graph_execution() -> None:
    with pytest.raises(ValidationError):
        RecommenderGraphInput(
            user_id=0,
            intent=_intent(),
            system_prompt="safe context",
            user_message="find events",
            thread_id="thread-1",
        )


def test_tool_registration_rejects_a_callable_without_model_facing_description() -> None:
    def undocumented() -> str:
        return "never registered"

    with pytest.raises(ValueError, match="requires a docstring"):
        build_langchain_tools({"undocumented": undocumented})
