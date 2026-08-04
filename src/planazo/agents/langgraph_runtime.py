"""Typed LangGraph runtime primitives for the Planazo Recommender and Extractor.

Owns the shared ``_GraphStateCore`` TypedDict, the topology-aware
recursion-limit helper, the framework's tool-registration adapter, and each
agent's graph builder + typed input model. The framework mechanics live here;
each agent's composition root (``event_agent.run_once`` for the Recommender,
``extractor.extract_once`` for the Extractor) continues to own Planazo policy:
identity binding, system-context assembly, typed preflight outcomes,
observability, ranking, calendar dispatch, and multimodal message injection.

Both graphs use custom ``StateGraph`` topologies rather than a high-level
agent constructor so the runtime state and edges stay visible and testable.
See ADR 0023 (Recommender) and ADR 0024 (Extractor).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, TypedDict, cast

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

RECOMMENDER_NODES_PER_CYCLE: Final[int] = 3
"""Node visits per model turn on the Recommender's cycle: agent, tools, enforce_step_cap."""

EXTRACTOR_NODES_PER_CYCLE: Final[int] = 4
"""Node visits per model turn on the Extractor cycle: agent, tools, post_tools, enforce_step_cap."""

RECURSION_SLACK: Final[int] = 2
"""Extra headroom on top of the cycle budget: the terminal answered turn plus one node."""


def recursion_limit_for(max_model_steps: int, nodes_per_cycle: int) -> int:
    """Return the LangGraph ``recursion_limit`` for a bounded model-turn loop.

    LangGraph counts every node visit against its recursion budget. A cycle of
    ``nodes_per_cycle`` nodes over ``max_model_steps`` model turns is
    ``max_model_steps * nodes_per_cycle`` node visits; adding
    ``RECURSION_SLACK`` leaves room for the terminal answered agent turn plus
    one node of headroom.
    """

    if max_model_steps < 1:
        raise ValueError("max_model_steps must be >= 1")
    if nodes_per_cycle < 2:
        raise ValueError("nodes_per_cycle must be >= 2")
    return max_model_steps * nodes_per_cycle + RECURSION_SLACK


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


class _GraphStateCore(TypedDict):
    """Fields every Planazo LangGraph state carries.

    ``messages`` is LangGraph's append-only message channel. The system prompt
    is intentionally separate from ``messages`` so callers can replace it with
    freshly assembled rules, preferences, and intent on a later turn without
    leaving an old prompt in the conversation history. Each concrete graph
    state inherits this base and adds its own domain fields on top.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    system_prompt: str
    model_steps: int
    max_model_steps: int
    stopped: Literal["running", "answered", "truncated", "max_steps"]


class PlanazoGraphState(_GraphStateCore):
    """Explicit persisted state for one Recommender graph thread.

    The application supplies every value through ``RecommenderGraphInput`` at
    the typed boundary; a resumed thread reloads this state from the
    configured saver.
    """

    user_id: int
    intent: SearchIntent
    thread_id: str


class ExtractorGraphState(_GraphStateCore):
    """Explicit per-run state for one Extractor graph invocation.

    The Extractor is single-shot: no ``thread_id``, no checkpointer. The
    composition root supplies ``url``, ``delegator_user_id``, and ``run_id``
    at the typed boundary via ``ExtractorGraphInput``.
    """

    url: str
    delegator_user_id: int
    run_id: str


class AgentNodeUpdate(TypedDict):
    """The only state fields the LLM node is permitted to update."""

    messages: list[BaseMessage]
    model_steps: int
    stopped: Literal["running", "answered", "truncated"]


class TerminalNodeUpdate(TypedDict):
    """The only state field the post-tool cap node may update."""

    stopped: Literal["running", "max_steps"]


class PostToolsUpdate(TypedDict):
    """The only state field the Extractor's ``post_tools`` node may update."""

    messages: list[BaseMessage]


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

        The recursion limit is computed by ``recursion_limit_for`` from the
        Recommender's ``RECOMMENDER_NODES_PER_CYCLE`` topology and the run's
        ``max_model_steps`` — one budget per node visit across the full
        ``agent -> tools -> enforce_step_cap`` cycle plus slack for the
        terminal answered turn.
        """

        return {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": recursion_limit_for(
                self.max_model_steps, RECOMMENDER_NODES_PER_CYCLE
            ),
        }


class ExtractorGraphInput(BaseModel):
    """Validated application input used to start one Extractor graph run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1, max_length=500)
    delegator_user_id: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(max_length=20_000)
    user_message: str = Field(min_length=1, max_length=4_000)
    max_model_steps: int = Field(default=32, ge=1, le=64)

    def initial_state(self) -> ExtractorGraphState:
        """Return the typed initial graph state for this extraction run."""

        return {
            "messages": [HumanMessage(content=self.user_message)],
            "url": self.url,
            "delegator_user_id": self.delegator_user_id,
            "run_id": self.run_id,
            "system_prompt": self.system_prompt,
            "model_steps": 0,
            "max_model_steps": self.max_model_steps,
            "stopped": "running",
        }

    def graph_config(self) -> RunnableConfig:
        """Return LangGraph configuration for one single-shot extraction run.

        The recursion limit is computed by ``recursion_limit_for`` from the
        Extractor's ``EXTRACTOR_NODES_PER_CYCLE`` topology and the run's
        ``max_model_steps`` — one budget per node visit across the full
        ``agent -> tools -> post_tools -> enforce_step_cap`` cycle plus slack
        for the terminal answered turn.
        """

        return {
            "recursion_limit": recursion_limit_for(self.max_model_steps, EXTRACTOR_NODES_PER_CYCLE),
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
        raise ValueError("the graph requires at least one tool")

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


def _message_text(message: AIMessage) -> str:
    """Project LangChain's validated message content onto Planazo's text contract."""

    if isinstance(message.content, str):
        return message.content
    return "".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in message.content
    )


def _agent_node(
    bound_model: Runnable[Sequence[BaseMessage], AIMessage],
    on_model_step: Callable[[int], None] | None,
) -> Callable[[_GraphStateCore], AgentNodeUpdate]:
    """Build the graph node that lets the LLM answer or request a tool."""

    def invoke_model(state: _GraphStateCore) -> AgentNodeUpdate:
        messages: list[BaseMessage] = list(state["messages"])
        if state["system_prompt"]:
            messages.insert(0, SystemMessage(content=state["system_prompt"]))
        response = bound_model.invoke(messages)
        if not isinstance(response, AIMessage):
            raise TypeError("the chat model must return an AIMessage")
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


def _route_after_agent(state: _GraphStateCore) -> Literal["tools", "__end__"]:
    """Dispatch only model-selected tools; otherwise terminate the graph."""

    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return cast(Literal["tools", "__end__"], END)


def _mark_max_steps(state: _GraphStateCore) -> TerminalNodeUpdate:
    """Keep the historical cap semantics after dispatching the final tools."""

    return {
        "stopped": ("max_steps" if state["model_steps"] >= state["max_model_steps"] else "running")
    }


def _route_after_tools(state: _GraphStateCore) -> Literal["agent", "__end__"]:
    """Return to the model only while the validated model-turn cap permits it."""

    return cast(Literal["agent", "__end__"], END if state["stopped"] == "max_steps" else "agent")


def _extractor_post_tools_node(
    post_tools: Callable[[ExtractorGraphState], list[BaseMessage]] | None,
) -> Callable[[ExtractorGraphState], PostToolsUpdate]:
    """Wrap the caller's post-tool hook (or a no-op) as a graph node.

    The Extractor's composition root owns the multimodal-injection seam via
    this hook. Installing a no-op passthrough when the caller omits the hook
    keeps the topology's four-node cycle honest so ``recursion_limit_for``
    remains a truth about the compiled graph rather than a truth about which
    hook happens to be wired.
    """

    def run(state: ExtractorGraphState) -> PostToolsUpdate:
        if post_tools is None:
            return {"messages": []}
        return {"messages": list(post_tools(state))}

    return run


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


def build_extractor_graph(
    model: ToolBindableChatModel,
    tools: Sequence[BaseTool],
    *,
    on_model_step: Callable[[int], None] | None = None,
    post_tools: Callable[[ExtractorGraphState], list[BaseMessage]] | None = None,
) -> CompiledStateGraph[ExtractorGraphState, None, ExtractorGraphState, ExtractorGraphState]:
    """Compile the Extractor's explicit LangGraph tool-calling topology.

    Topology: ``START -> agent -> (tools if tool_calls else END) -> post_tools
    -> enforce_step_cap -> (agent if running else END)``. The ``post_tools``
    node is the Planazo-side injection seam — the Extractor's composition
    root wraps its multimodal ``HumanMessage``-append hook here; when the
    caller supplies no hook, a no-op passthrough node keeps the topology's
    four-node cycle honest for ``recursion_limit_for`` accounting.
    """

    if not tools:
        raise ValueError("the Extractor graph requires at least one LangChain tool")

    bound_model = model.bind_tools(tools)
    builder = StateGraph(ExtractorGraphState)
    agent_node = cast(
        Runnable[ExtractorGraphState, object],
        _agent_node(bound_model, on_model_step),
    )
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(list(tools)))
    builder.add_node(
        "post_tools",
        cast(
            Runnable[ExtractorGraphState, object],
            _extractor_post_tools_node(post_tools),
        ),
    )
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
    builder.add_edge("tools", "post_tools")
    builder.add_edge("post_tools", "enforce_step_cap")
    builder.add_conditional_edges(
        "enforce_step_cap",
        _route_after_tools,
        {
            "agent": "agent",
            END: END,
        },
    )
    return builder.compile()


def invoke_extractor_graph(
    graph: CompiledStateGraph[ExtractorGraphState, None, ExtractorGraphState, ExtractorGraphState],
    request: ExtractorGraphInput,
) -> ExtractorGraphState:
    """Invoke a compiled Extractor graph from validated application input.

    The cast is confined to the framework boundary: LangGraph returns a
    mapping, while every public input was built by ``ExtractorGraphInput``
    and the graph can emit only keys declared in ``ExtractorGraphState``.
    ``extractor.extract_once`` projects the final state onto ``ExtractionResult``.
    """

    state = graph.invoke(request.initial_state(), config=request.graph_config())
    return cast(ExtractorGraphState, state)
