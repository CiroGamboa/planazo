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
    EXTRACTOR_NODES_PER_CYCLE,
    RECOMMENDER_NODES_PER_CYCLE,
    PlanazoGraphState,
    RecommenderGraphInput,
    build_langchain_tools,
    build_recommender_graph,
    invoke_recommender_graph,
    open_recommender_checkpointer,
    recursion_limit_for,
)
from planazo.query.models import SearchIntent


def _intent() -> SearchIntent:
    start = datetime(2026, 8, 3, 18, tzinfo=UTC)
    return SearchIntent(start_utc=start, end_utc=start + timedelta(hours=4), city="Barcelona")


def _uppercase(text: str) -> str:
    """Return text in uppercase for a deterministic graph-tool test."""

    return text.upper()


def _search_events(city: str) -> dict[str, object]:
    """Search events for the requested city in this deterministic graph test."""

    return {"events": [], "city": city}


def _retrieve_memory(query: str) -> dict[str, object]:
    """Retrieve saved user-memory context for this deterministic graph test."""

    return {"facts": [f"remembered: {query}"]}


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


class QuerySelectingModel:
    """Fake model that selects a registered tool from the user's query."""

    def __init__(self) -> None:
        self.bound_tools: Sequence[BaseTool] = ()
        self.selected_tools: list[str] = []

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable[Sequence[BaseMessage], AIMessage]:
        self.bound_tools = tools
        return self  # type: ignore[return-value]

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="Tool result considered.")
        user_text = next(
            str(message.content).casefold()
            for message in reversed(messages)
            if message.type == "human"
        )
        if "preference" in user_text:
            self.selected_tools.append("retrieve_memory")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve_memory",
                        "args": {"query": "quiet venues"},
                        "id": "memory-call",
                    }
                ],
            )
        self.selected_tools.append("search_events")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_events",
                    "args": {"city": "Barcelona"},
                    "id": "search-call",
                }
            ],
        )


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


def test_typed_graph_state_has_no_ad_hoc_mapping_fields() -> None:
    annotations = PlanazoGraphState.__annotations__

    assert set(annotations) == {
        "messages",
        "user_id",
        "intent",
        "system_prompt",
        "thread_id",
        "model_steps",
        "max_model_steps",
        "stopped",
    }
    assert all("dict" not in str(annotation).lower() for annotation in annotations.values())


def test_fake_model_selects_registered_search_and_memory_tools_through_tool_node() -> None:
    dispatched: list[str] = []

    def search_events(city: str) -> dict[str, object]:
        dispatched.append("search_events")
        return _search_events(city)

    def retrieve_memory(query: str) -> dict[str, object]:
        dispatched.append("retrieve_memory")
        return _retrieve_memory(query)

    search_events.__doc__ = _search_events.__doc__
    retrieve_memory.__doc__ = _retrieve_memory.__doc__
    model = QuerySelectingModel()
    graph = build_recommender_graph(
        model,
        build_langchain_tools(
            {
                "search_events": search_events,
                "retrieve_memory": retrieve_memory,
            }
        ),
    )

    event_state = invoke_recommender_graph(
        graph,
        _request().model_copy(update={"user_message": "Find events in Barcelona."}),
    )
    preference_state = invoke_recommender_graph(
        graph,
        _request().model_copy(update={"user_message": "Use my preference for quiet venues."}),
    )

    assert {tool.name for tool in model.bound_tools} == {"search_events", "retrieve_memory"}
    assert model.selected_tools == ["search_events", "retrieve_memory"]
    assert dispatched == ["search_events", "retrieve_memory"]
    assert any(isinstance(message, ToolMessage) for message in event_state["messages"])
    assert any(isinstance(message, ToolMessage) for message in preference_state["messages"])


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

    assert request.graph_config()["recursion_limit"] == recursion_limit_for(1, 3)
    assert request.graph_config()["recursion_limit"] == 5

    state = invoke_recommender_graph(graph, request)

    assert state["model_steps"] == 1
    assert state["stopped"] == "max_steps"
    assert any(isinstance(message, ToolMessage) for message in state["messages"])


def test_recommender_graph_reaches_step_cap_at_max_model_steps_eight() -> None:
    graph = build_recommender_graph(
        RepeatedToolModel(),
        build_langchain_tools({"uppercase": _uppercase}),
    )
    request = _request().model_copy(update={"max_model_steps": 8})

    state = invoke_recommender_graph(graph, request)

    assert state["stopped"] == "max_steps"
    assert state["model_steps"] == 8


@pytest.mark.parametrize(
    ("max_model_steps", "nodes_per_cycle", "expected_limit"),
    [
        (8, RECOMMENDER_NODES_PER_CYCLE, 26),
        (32, EXTRACTOR_NODES_PER_CYCLE, 130),
        (1, RECOMMENDER_NODES_PER_CYCLE, 5),
        (1, EXTRACTOR_NODES_PER_CYCLE, 6),
        (64, EXTRACTOR_NODES_PER_CYCLE, 258),
    ],
)
def test_recursion_limit_for_is_topology_aware(
    max_model_steps: int, nodes_per_cycle: int, expected_limit: int
) -> None:
    assert recursion_limit_for(max_model_steps, nodes_per_cycle) == expected_limit


@pytest.mark.parametrize("bad_max_model_steps", [0, -1])
def test_recursion_limit_for_rejects_non_positive_max_model_steps(
    bad_max_model_steps: int,
) -> None:
    with pytest.raises(ValueError, match="max_model_steps must be >= 1"):
        recursion_limit_for(bad_max_model_steps, RECOMMENDER_NODES_PER_CYCLE)


@pytest.mark.parametrize("bad_nodes_per_cycle", [0, 1, -1])
def test_recursion_limit_for_rejects_degenerate_topology(bad_nodes_per_cycle: int) -> None:
    with pytest.raises(ValueError, match="nodes_per_cycle must be >= 2"):
        recursion_limit_for(1, bad_nodes_per_cycle)


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
