# ADR 0024 — LangGraph Extractor runtime

**Status:** Proposed
**Date:** 2026-08-03
**Deciders:** Planazo team
**Related:** [ADR 0002](0002-event-tool-contracts-and-approval-gate.md), [ADR 0005](0005-multi-agent-shape.md), [ADR 0006](0006-instagram-extraction-approach.md), [ADR 0013](0013-extractor-side-frame-extraction.md), [ADR 0023](0023-langgraph-recommender-runtime.md).

## Context

ADR 0023 accepted the LangGraph runtime for the Recommender only; the Curator and every other agent stayed on the hand-written `run_loop` in `planazo.agents.loop`. This ADR extends the runtime boundary to the Extractor. The Curator and any other consumer of `planazo.agents.loop.run_loop` remain out of scope.

`extract_once(url, delegator_user_id, ...) -> ExtractionResult` currently drives the hand-written observe → reason → act → verify loop directly. The Agentic AI Systems HW1 requires both first-half agents — Recommender and Extractor — on a framework runtime with an explicit typed state schema and LangChain-registered tools. The Extractor's existing boundaries must survive the port: the three-tool registry (`fetch_instagram_post`, `save_event`, `report_extraction_status`), the multimodal `HumanMessage` append that runs after a successful `fetch_instagram_post` call, the `MAX_STEPS=32` step cap, the `MAX_OUTPUT_TOKENS=2000` model cap, the trace-inspection terminal detection, and the ADR 0002 / ADR 0005 §4 rule that `save_event` is a reversible catalog write and therefore is not gated by an `ApprovalGate`.

## Decision

The Extractor runtime will move to a custom LangGraph `StateGraph` peer to the Recommender's. Its graph state will be an explicit `TypedDict` — `ExtractorGraphState` — carrying the message history, the source URL, the delegator user id, the run identifier, the system prompt, the step accounting, and a typed `stopped` literal. Extractor tools will be registered as LangChain tools bound to the model; the framework — not application branching — dispatches the tool call the model selects. The composition root, `extract_once`, keeps its public signature and continues to return `ExtractionResult`.

What stays exactly as it is today:

- **No checkpointing.** The Extractor is single-shot per URL. There is no `thread_id` on `ExtractorGraphInput` and no SQLite saver on its graph. Extractor resume is out of scope for this ADR.
- **No `ApprovalGate` on `save_event`.** `save_event` remains a reversible catalog write (ADR 0002, ADR 0005 §4). The Extractor's tool binding does not accept a gate argument.
- **`MAX_STEPS=32` and `MAX_OUTPUT_TOKENS=2000`.** Both constants keep their current values and are passed through to the graph's `max_model_steps` and to the `ChatOpenAI` factory respectively.
- **Three-tool registry.** The bound tool set is exactly `{fetch_instagram_post, save_event, report_extraction_status}`. `search_events` is deliberately excluded — the Extractor never queries the catalog.
- **Multimodal `input_image` append after `fetch_instagram_post`.** After the framework's `ToolNode` runs a successful `fetch_instagram_post` call, the graph emits one additional `HumanMessage` whose content parts carry the fetched post's visual assets — a single image, up to `profile.max_carousel_images` carousel slides, a reel-frame array with the thumbnail as cover, a thumbnail fallback, or a "no visual asset" note. Non-fetch tool calls emit nothing.

The multimodal HumanMessage-append behaviour is grafted onto the graph as a new node between `ToolNode` and the step-cap node. The graph itself owns tool dispatch — the `ToolNode` runs the LangChain-registered callable and appends the resulting `ToolMessage` to the message list. The Extractor's composition root owns the node factory that wraps `_build_multimodal_hook`; the factory closes over the URL, the resolved multimodal profile, the `on_multimodal_send` observer, and a mutable queue that the instrumented `fetch_instagram_post` wrapper appends `StepRecord`s to on each successful dispatch. The `post_tools` node drains the queue and returns the resulting `HumanMessage`s for LangGraph to append.

The topology-aware recursion-budget rule for both graphs is `recursion_limit = max_model_steps * nodes_per_cycle + slack`. The Extractor's 4-node cycle (`agent → tools → post_tools → enforce_step_cap`) needs `max_model_steps * 4 + slack`; the Recommender's 3-node cycle (`agent → tools → enforce_step_cap`) needs `max_model_steps * 3 + slack`. The previous `RecommenderGraphInput.graph_config()` formula — `max_model_steps * 2 + 2` — is an off-by-cycle-node latent bug: at the CLI default `max_model_steps=8` it yields `18`, undersized for the roughly `~24` supersteps the 3-node cycle needs to reach the step cap; the bug hides only because the Recommender's tests exercise `max=1`. This ADR captures the fix at both call sites in the same PR: a shared `recursion_limit_for(...)` helper replaces the buggy formula at the Recommender's call site and is the sole source of truth at the Extractor's call site.

## Consequences

- The Extractor gains framework-managed tool dispatch, explicit graph state, and a LangChain tool boundary while preserving every existing Pydantic contract, typed-error branch, and observability seam (`on_step`, `on_complete`, `on_multimodal_send`, `ExtractionRunLogger`, `AgentRunLogger`, `LLMDecisionLogger`).
- The three Extractor tools — `fetch_instagram_post`, `save_event`, `report_extraction_status` — are demonstrably registered as LangChain tools whose invocation is selected by the LLM.
- The repository does not gain new runtime dependencies. LangGraph, LangChain, and the LangChain-OpenAI integration are already runtime dependencies from ADR 0023; the SQLite checkpoint package remains unused by the Extractor because the Extractor is single-shot.
- Terminal state detection remains trace-derived, not name-matched: the composition root inspects the trace to project the graph's terminal `AIMessage` and its tool-call history to `ExtractionResult`. The graph itself does not short-circuit on `save_event` or `report_extraction_status` tool names.
- The Recommender's `graph_config().recursion_limit` increases (goes from `max_model_steps * 2 + 2` to `max_model_steps * 3 + slack`). This is an internal LangGraph knob, not a persisted or user-visible surface; no checkpoint schema changes.
- `AGENTS.md` rule 5 must be rewritten in the same PR that lands the port so it names both framework runtimes (Recommender + Extractor) and continues to hold every other agent out of scope. That edit lands in Stage 3, not this stage.

## Rejected alternatives

1. **Keep the hand-written `run_loop` for the Extractor.** Rejected because HW1 requires both first-half agents on a framework runtime, and leaving the Extractor on the hand-written loop would satisfy the ticket only on paper.
2. **Share the Recommender's `PlanazoGraphState` with a widened schema.** Rejected because the Extractor's per-run state carries different domain fields (`url`, `delegator_user_id`, `run_id`) and the Recommender's `thread_id` + `intent` do not apply to a single-shot URL extraction. A widened union would erode both agents' type contracts. Instead, both graphs' TypedDicts inherit a shared `_GraphStateCore` base that owns only the structural fields (`messages`, `system_prompt`, `model_steps`, `max_model_steps`, `stopped`), and each agent's TypedDict adds its own domain fields on top.
3. **Add an `ApprovalGate` to `save_event` when porting.** Rejected because ADR 0002 and ADR 0005 §4 explicitly classify `save_event` as a reversible catalog write. Gating it would break the Extractor's non-interactive contract and contradict two prior ADRs without a superseding decision.
4. **Add SQLite checkpointing to the Extractor for parity with the Recommender.** Rejected because the Extractor is single-shot per URL; there is no user-facing resumable conversation to persist. A checkpointer would add I/O and a schema surface for a capability nothing consumes. If a future ticket asks for extractor resume, it lands separately.
5. **Move the multimodal append into a monkey-patched `ToolNode` subclass.** Rejected because the graph's tool node is a framework primitive and subclassing it to inject application semantics would blur the graph-vs-composition-root boundary. A dedicated `post_tools` node between `ToolNode` and the step-cap node keeps the graph topology honest — the recursion-limit formula is a truth about the topology, so a passthrough no-op node is installed when the injection hook happens to be `None`, keeping the 4-node cycle count intact.

## Follow-up work

- Stage 2: extend `src/planazo/agents/langgraph_runtime.py` with the shared `_GraphStateCore`, the topology-aware `recursion_limit_for(...)` helper, the Extractor primitives (`ExtractorGraphState`, `ExtractorGraphInput`, `build_extractor_graph`, `invoke_extractor_graph`), and lift the state-agnostic node/router helpers so both graphs share them. Fix the Recommender's `graph_config()` at the same call site in the same commit.
- Stage 3: rewrite `extract_once` onto `build_extractor_graph` + `invoke_extractor_graph`, flip this ADR's Status to Accepted, and rewrite `AGENTS.md` rule 5 to name both framework runtimes.
- Stage 4: extend the README's `## LangGraph Recommender runtime` section into `## LangGraph agent runtimes` with a peer Extractor subsection and a Mermaid flow diagram.
