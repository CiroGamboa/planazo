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
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypedDict, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, ConfigDict, Field

from planazo.query.models import SearchIntent

DEFAULT_CHECKPOINT_PATH = Path("data/langgraph/recommender-checkpoints.sqlite3")


def recommender_checkpoint_path(path: str | Path | None = None) -> Path:
    """Return the local, non-domain SQLite database used for graph state."""

    resolved = Path(path) if path is not None else DEFAULT_CHECKPOINT_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def open_recommender_checkpointer(
    path: str | Path | None = None,
) -> AbstractContextManager[SqliteSaver]:
    """Open a durable LangGraph SQLite saver outside Planazo's domain store."""

    return SqliteSaver.from_conn_string(str(recommender_checkpoint_path(path)))


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
    max_model_steps: int
    stopped: Literal["running", "answered", "truncated", "max_steps"]


class AgentNodeUpdate(TypedDict):
    """The only state fields the LLM node is permitted to update."""

    messages: list[BaseMessage]
    model_steps: int
    stopped: Literal["running", "answered", "truncated"]


class TerminalNodeUpdate(TypedDict):
    """The only state field the post-tool cap node may update."""

    stopped: Literal["running", "max_steps"]


class RecommenderGraphInput(BaseModel):
    """Validated application input used to start or continue one graph turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: int = Field(ge=1)
    intent: SearchIntent
    system_prompt: str = Field(max_length=20_000)
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
            "max_model_steps": self.max_model_steps,
            "stopped": "running",
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
            "recursion_limit": self.max_model_steps * 2 + 2,
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
    on_model_step: Callable[[int], None] | None,
) -> Callable[[PlanazoGraphState], AgentNodeUpdate]:
    """Build the graph node that lets the LLM answer or request a tool."""

    def invoke_model(state: PlanazoGraphState) -> AgentNodeUpdate:
        messages: list[BaseMessage] = list(state["messages"])
        if state["system_prompt"]:
            messages.insert(0, SystemMessage(content=state["system_prompt"]))
        response = bound_model.invoke(messages)
        if not isinstance(response, AIMessage):
            raise TypeError("the Recommender chat model must return an AIMessage")
        next_step = state["model_steps"] + 1
        if on_model_step is not None:
            on_model_step(next_step)
        finish_reason = response.response_metadata.get("finish_reason")
        stopped: Literal["running", "answered", "truncated"] = (
            "truncated" if finish_reason in {"length", "max_output_tokens"} else "answered"
        )
        if response.tool_calls:
            stopped = "running"
        return {
            "messages": [response],
            "model_steps": next_step,
            "stopped": stopped,
        }

    return invoke_model


def _route_after_agent(state: PlanazoGraphState) -> Literal["tools", "__end__"]:
    """Dispatch only model-selected tools; otherwise terminate the graph."""

    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return cast(Literal["tools", "__end__"], END)


def _mark_max_steps(state: PlanazoGraphState) -> TerminalNodeUpdate:
    """Keep the historical cap semantics after dispatching the final tools."""

    return {
        "stopped": ("max_steps" if state["model_steps"] >= state["max_model_steps"] else "running")
    }


def _route_after_tools(state: PlanazoGraphState) -> Literal["agent", "__end__"]:
    """Return to the model only while the validated model-turn cap permits it."""

    return cast(Literal["agent", "__end__"], END if state["stopped"] == "max_steps" else "agent")


def build_recommender_graph(
    model: ToolBindableChatModel,
    tools: Sequence[BaseTool],
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    on_model_step: Callable[[int], None] | None = None,
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
        _agent_node(bound_model, on_model_step),
    )
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(list(tools)))
    builder.add_node("enforce_step_cap", _mark_max_steps)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        _route_after_agent,
        {
            "tools": "tools",
            END: END,
        },
    )
    builder.add_edge("tools", "enforce_step_cap")
    builder.add_conditional_edges(
        "enforce_step_cap",
        _route_after_tools,
        {
            "agent": "agent",
            END: END,
        },
    )
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
