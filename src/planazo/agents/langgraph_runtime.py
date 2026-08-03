"""Typed LangGraph runtime primitives for the Planazo Recommender.

This module owns framework mechanics only: the explicit graph state, LangChain
tool registration, and the ``agent -> tools -> agent`` topology. The existing
``event_agent.run_once`` composition root continues to own Planazo policy:
identity binding, system-context assembly, typed preflight outcomes,
observability, ranking, and approval-gated calendar actions.

The graph is deliberately custom rather than a high-level agent constructor so
the runtime state and edges stay visible and testable. See ADR 0023.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Protocol, TypedDict, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, ConfigDict, Field

from planazo.query.models import SearchIntent


class PlanazoGraphState(TypedDict):
    """Explicit persisted state for one Recommender graph thread.

    ``messages`` is LangGraph's append-only message channel. The application
    supplies every other value through ``RecommenderGraphInput`` at the typed
    boundary; a resumed thread reloads this state from the configured saver.
    The system prompt is intentionally separate from ``messages`` so callers
    can replace it with freshly assembled rules, preferences, and intent on a
    later turn without leaving an old prompt in the conversation history.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    user_id: int
    intent: SearchIntent
    system_prompt: str
    thread_id: str
    model_steps: int


class AgentNodeUpdate(TypedDict):
    """The only state fields the LLM node is permitted to update."""

    messages: list[BaseMessage]
    model_steps: int


class RecommenderGraphInput(BaseModel):
    """Validated application input used to start or continue one graph turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: int = Field(ge=1)
    intent: SearchIntent
    system_prompt: str = Field(min_length=1, max_length=20_000)
    user_message: str = Field(min_length=1, max_length=4_000)
    thread_id: str = Field(min_length=1, max_length=200)
    max_model_steps: int = Field(default=8, ge=1, le=32)

    def initial_state(self) -> PlanazoGraphState:
        """Return the typed initial graph update for this user message."""

        return {
            "messages": [HumanMessage(content=self.user_message)],
            "user_id": self.user_id,
            "intent": self.intent,
            "system_prompt": self.system_prompt,
            "thread_id": self.thread_id,
            "model_steps": 0,
        }

    def graph_config(self) -> RunnableConfig:
        """Return LangGraph configuration with a durable, bounded thread.

        LangGraph counts both the model and tool nodes against its recursion
        budget. Two graph steps per allowed model turn plus one terminal model
        step preserves the Recommender's bounded-loop policy while allowing the
        framework to own routing.
        """

        return {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": self.max_model_steps * 2 + 1,
        }


class ToolBindableChatModel(Protocol):
    """The narrow LangChain chat-model surface this graph needs."""

    def bind_tools(
        self, tools: Sequence[BaseTool]
    ) -> Runnable[Sequence[BaseMessage], AIMessage]: ...


ToolCallable = Callable[..., object]


def build_langchain_tools(registry: Mapping[str, ToolCallable]) -> list[BaseTool]:
    """Turn Planazo's already-bound callables into LangChain ``StructuredTool``s.

    The callable's signature becomes the framework-managed Pydantic argument
    schema, while the callable itself retains its existing domain validation,
    identity closure, and typed result contract. A missing docstring would
    leave the model without a safe tool description, so it is rejected at
    composition time rather than silently registered.
    """

    if not registry:
        raise ValueError("the Recommender graph requires at least one tool")

    tools: list[BaseTool] = []
    for name, function in registry.items():
        if not name:
            raise ValueError("tool names must not be empty")
        description = inspect.getdoc(function)
        if not description:
            raise ValueError(f"tool {name!r} requires a docstring")
        tools.append(
            StructuredTool.from_function(
                func=function,
                name=name,
                description=description,
            )
        )
    return tools


def _agent_node(
    bound_model: Runnable[Sequence[BaseMessage], AIMessage],
) -> Callable[[PlanazoGraphState], AgentNodeUpdate]:
    """Build the graph node that lets the LLM answer or request a tool."""

    def invoke_model(state: PlanazoGraphState) -> AgentNodeUpdate:
        response = bound_model.invoke(
            [SystemMessage(content=state["system_prompt"]), *state["messages"]]
        )
        if not isinstance(response, AIMessage):
            raise TypeError("the Recommender chat model must return an AIMessage")
        return {
            "messages": [response],
            "model_steps": state["model_steps"] + 1,
        }

    return invoke_model


def build_recommender_graph(
    model: ToolBindableChatModel,
    tools: Sequence[BaseTool],
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[PlanazoGraphState, None, PlanazoGraphState, PlanazoGraphState]:
    """Compile the Recommender's explicit LangGraph tool-calling topology."""

    if not tools:
        raise ValueError("the Recommender graph requires at least one LangChain tool")

    bound_model = model.bind_tools(tools)
    builder = StateGraph(PlanazoGraphState)
    # LangGraph accepts a partial state update here. Its current type stubs model
    # nodes as generic runnables, so preserve that framework boundary explicitly.
    agent_node = cast(
        Runnable[PlanazoGraphState, object],
        _agent_node(bound_model),
    )
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(list(tools)))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        tools_condition,
        {
            "tools": "tools",
            END: END,
        },
    )
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


def invoke_recommender_graph(
    graph: CompiledStateGraph[PlanazoGraphState, None, PlanazoGraphState, PlanazoGraphState],
    request: RecommenderGraphInput,
) -> PlanazoGraphState:
    """Invoke a compiled graph from validated application input.

    The cast is confined to the framework boundary: LangGraph returns a
    mapping, while every public input was built by ``RecommenderGraphInput``
    and the graph can emit only keys declared in ``PlanazoGraphState``.
    ``event_agent`` will validate the final domain-facing result separately.
    """

    state = graph.invoke(request.initial_state(), config=request.graph_config())
    return cast(PlanazoGraphState, state)
