# ADR 0023 — LangGraph Recommender runtime

**Status:** Accepted
**Date:** 2026-08-03
**Deciders:** Planazo team
**Related:** [ADR 0001](0001-agent-runtime-layout-and-provider.md), [ADR 0004](0004-three-store-memory-model.md), [ADR 0016](0016-multi-turn-recommender-conversation.md), [ADR 0021](0021-recommender-tool-boundary-shrink.md).

## Context

Planazo's Recommender currently owns a hand-written observe → reason → act → verify loop in `planazo.agents.loop`, while `event_agent.run_once` composes the bound tool registry, push context, typed preflight outcomes, observability, and deterministic ranking. The Agentic AI Systems HW1 requires a Python LangGraph implementation with LangChain tools, an explicit state schema, at least two LLM-selected tools, and checkpointed conversation resumption.

The refactor must not weaken Planazo's existing boundaries: `SearchIntent`, events, identity-bound memory access, tool results, and public `RecommenderResult` remain validated models; raw retrieved text remains data rather than instructions; and approval-gated calendar actions stay explicitly gated.

## Decision

The Recommender runtime will move to a custom LangGraph `StateGraph`. Its graph state will be an explicit `TypedDict`, with typed fields for the message history, bound user, current intent, system context, turn identifier, and step accounting. The graph will contain an LLM node, a framework-owned tool node, and a conditional edge back to the LLM only when the LLM emitted a tool call.

Recommender tools will be registered as LangChain tools. They retain the current application-owned closures and Pydantic validation: the LLM never receives a `user_id` tool argument, and no framework default may replace typed failure outcomes or the approval gate.

LangGraph checkpoints will use a persistent SQLite saver in a gitignored runtime file. Callers supply a stable `thread_id`; resuming the same thread reloads the graph state after a process restart. An in-memory saver is permitted only in isolated tests, never as the runnable application's checkpoint mechanism.

`ChatOpenAI` will use Planazo's existing OpenAI-compatible OpenCode Zen endpoint and `OPENCODE_API_KEY`, with a low-cost tool-call compatibility probe before the runtime migration. If that probe identifies a provider incompatibility, the team must record and resolve it before replacing the Recommender loop.

The existing generic loop remains available to agents that still use it, including the Extractor, until they receive their own explicit migration decision. `event_agent.run_once` remains the Recommender's public composition root and continues to return `RecommenderResult`.

## Consequences

- The Recommender gains framework-managed tool dispatch, explicit graph state, and durable checkpoint resumption while preserving Planazo's product contracts.
- At least `search_events` and `retrieve_memory` will be demonstrably registered LangChain tools whose use is selected by the LLM; `ask_user` remains a typed, non-blocking clarification tool.
- The repository gains LangGraph, LangChain, LangChain's OpenAI integration, and the SQLite checkpoint package as runtime dependencies.
- Checkpoint state can contain user messages and structured tool outputs. It is local runtime data, is gitignored, and must remain separated by `thread_id`.

## Rejected alternatives

1. **Keep the hand-written Recommender loop.** Rejected because it cannot satisfy the homework's framework-integration requirement.
2. **Use LangChain's high-level agent constructor.** Rejected because a custom `StateGraph` makes Planazo's state, nodes, edges, and tool boundary visible and testable.
3. **Use an in-memory checkpoint saver in the application.** Rejected because it cannot resume a conversation after process restart.
4. **Replace every Planazo agent at once.** Rejected because the homework targets the Recommender; migrating the Extractor or Curator would expand risk without improving the required demonstration.

## Follow-up work

- Implement the graph runtime and its LangChain tool adapters.
- Add persistent-checkpoint and LLM-selected-tool tests, including cross-thread isolation and resume after graph re-creation.
- Update the README with final setup, tool, and Mermaid flow documentation once the runtime is executable.
