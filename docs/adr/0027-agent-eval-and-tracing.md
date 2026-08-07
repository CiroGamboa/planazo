# 0027 — Agent evaluation, MLflow tracing, and safety adapter shape (HW4)

- **Status:** Accepted
- **Date:** 2026-08-07
- **Deciders:** cirogam22
- **Relates to:** [`0023-langgraph-recommender-runtime.md`](0023-langgraph-recommender-runtime.md) (LangGraph is what autolog attaches to), [`0025-rag-over-events.md`](0025-rag-over-events.md) (the retrieval + generation scorers this ADR wires into MLflow feedback), [`0004-three-store-memory-model.md`](0004-three-store-memory-model.md) + [`0002-event-tool-contracts-and-approval-gate.md`](0002-event-tool-contracts-and-approval-gate.md) (Layer 2 + Layer 4 defenses this ADR cites rather than reimplements).

## Context

The Agentic AI Systems course's HW4 asks the team to demonstrate three things over the Recommender:

1. an agent-evaluation suite: ≥10 scenarios, ≥2 trajectory metrics, 3-run reliability with `pass@3` and `pass^3`, one scenario intentionally flaky;
2. tracing that lets a later reader pull the recommender's span tree (root, LLM calls, tool executions, retrieval steps) with model / token / latency visible on the spans, and tags (`request_origin`, `eval_case_id`) so an eval trace is findable and joinable back to the case that produced it;
3. a scorer feedback loop: the retrieval + generation scorers already in the repo (`src/planazo/eval/metrics/*.py`, HW3 landing PR #142) run over stored traces and write their results via `mlflow.log_feedback(...)`, with the scorer bodies untouched.

An optional Part 3 asks for a safety layer wired to the same traces.

The class ordering guidance: trace the agent first, then the Part 1 trajectory capture is a ~20-line adapter over traces you already have. If Part 1 lands against a hand-rolled log first, the capture step gets written twice. So this ADR settles the tracing shape before the evaluation shape.

Six load-bearing decisions. Each names at least one rejected alternative.

**1. MLflow (not Phoenix, LangSmith, or a hand-rolled JSONL).**
Free, local file-store backend by default (`file:./var/mlflow`), native `langchain.autolog()` integration, first-class `@mlflow.trace` decorator for the two spans autolog does not reach, and a `log_feedback(...)` API meant for exactly the "run a scorer against a stored trace" pattern. The class covered MLflow in Session 12, so a reader can go from the notebook to this ADR without re-learning tooling.

*Alternatives rejected:*

- **Phoenix / Arize.** Excellent UI but the local storage story is heavier; MLflow's file store fits `docker compose up`.
- **LangSmith.** Requires a network trip on every trace, needs a paid tier for group access, moves eval data off-repo.
- **Hand-rolled JSONL traces** on top of the existing `RunStepLogger`. Feasible — but writes the trace shape twice, once for humans (`var/*.jsonl`) and once for the eval adapters. That is the exact duplication the class warned against.

**2. `mlflow.langchain.autolog()` for LLM + TOOL spans; hand-rolled `@mlflow.trace` for the root and the RAG retrieval.**
LangGraph auto-instrumentation covers CHAT_MODEL and TOOL spans natively, so we get 12 of the 14 spans in a real run for free. The two hand-rolled decorators are on `agents.event_agent.run_once` (root AGENT span per invocation) and `catalog.rag.search_events_rag` (the RETRIEVER span whose output carries the ranked event ids the scorers consume).

*Alternatives rejected:*

- **Fully hand-rolled instrumentation** (a `@mlflow.trace` on every tool wrapper). Duplicates what autolog gives us for free and breaks the moment a tool is added or renamed.
- **No autolog, just a root span.** Nothing to attach the scorer feedback to; every span except the root would be invisible.

**3. Three-tag convention: `request_origin`, `eval_case_id`, `agent_kind`.**
`request_origin ∈ {bot, cli, eval, batch}` separates production traffic from eval and batch scorer runs — the class calls this out explicitly. `eval_case_id` is the join key from the scenario file to a trace (bounded cardinality by design, so Session-12's "high-cardinality tag" warning does not apply). `agent_kind ∈ {recommender, curator, extractor}` lets us keep all three agents in one experiment.

Tags are set through helpers in `src/planazo/observability/tracing.py` (`set_request_origin`, `set_eval_case_id`, `set_agent_kind`) that internally call `mlflow.update_current_trace(tags=...)`. Composition roots do not import `mlflow` directly, and the tag helpers no-op safely when there is no active trace (test isolation, MLflow offline). `run_once` reads `request_origin` and `eval_case_id` from `run_context` and calls the helpers itself — callers pass those in as kwargs and never touch the tracer.

*Alternatives rejected:*

- **Store `eval_case_id` as a metric, not a tag.** MLflow metrics are numeric; a string case id would need a lookup table. Tags are what UI search reads.
- **Free-form `.set_tag()` at each call site.** Too many places to forget one; a helper module gives one obvious place to change the tag surface later.

**4. Scorer bodies are frozen; adapters are the only glue.**
The retrieval scorers (`hit_at_k`, `precision_at_k`, `recall_at_k`, `mrr`, `ndcg_at_k`) and generation scorers (`score_faithfulness`, `score_answer_relevance`, `score_context_precision`) do not change. All wiring lives in three pure functions in `src/planazo/eval/agent/adapters.py`:

- `trace_to_tool_calls(trace) -> list[ToolCall]` — TOOL spans, sorted by start_time_ns, name + arguments only (never results — the assignment's tool-selection metric is argument-only for a reason).
- `trace_to_retrieval_inputs(trace) -> list[str] | None` — the RETRIEVER span's output as ranked event ids.
- `trace_to_generation_inputs(trace) -> tuple[query, answer, chunks] | None` — the three fields the generation scorers need.

If a scorer body would need to change to work with a trace, the adapter is doing too little. That is the class-taught invariant that makes Session 11's scorers and Session 12's traces click together.

*Alternative rejected:*

- **Change each scorer to accept a `trace` argument.** Would spread MLflow's Trace type across every scorer signature and permanently couple the RAG-eval to MLflow. The adapter isolates that dependency to one file.

**Note on `mlflow.log_feedback`.** MLflow 2.22 ships the `log_feedback` API but the open-source file backend raises "This API is currently only available for Databricks Managed MLflow" when you call it — the client stub is present, the server side is not. We fall back to `MlflowClient().set_trace_tag(trace_id, "feedback.<metric>", str(value))` — the trace still carries the scorer result, MLflow UI shows it in the tag column, and downstream tools can filter or aggregate on the tag prefix. When the open-source backend catches up, `scripts/run_trace_scorers.py` swaps back to `log_feedback` in one place.

**5. Tokens: acknowledged gap, tiktoken fallback.**
HW4 asks token counts to be visible on relevant spans. LangChain autolog surfaces token counts when the model provider returns them in the response body. Our provider (OpenCode Zen, ADR 0001) does not, so `token_usage` on the CHAT_MODEL span is `None`. The tracing module ships an `estimate_tokens(text)` helper (tiktoken, with `cl100k_base` fallback for unrecognized model ids) and a `set_token_usage(input_text, output_text)` helper that writes an approximate `{input_tokens, output_tokens, total_tokens, source: "tiktoken_estimate"}` blob into trace metadata. The HW4 report calls this out explicitly — the token count is an estimate marked as such, not a real usage record.

*Alternative rejected:*

- **Route through a provider that reports usage.** Would fork the runtime just for the eval — an option the assignment explicitly warns against ("if you edit the scorer to make it fit, the adapter is doing too little").

**6. Safety: two live layers plus two cited ones.**
Layer 1 (input filtering: injection pattern detection) and Layer 3 (output filtering: exfiltration + secret detection) get real code in `src/planazo/safety/detect.py`. Layer 2 (structural separation: retrieved text never joins the system role) is already enforced by AGENTS.md rule 2 and `event_agent.py`'s push-context assembly (ADR 0004); Layer 4 (capability constraints: the memory-tool user-id closure and the approval-gate on irreversible tools) is already enforced by ADR 0002 + ADR 0004. Layers 2 and 4 are cited in the report and the safety detector, not reimplemented — reimplementing them would fork existing invariants.

The detector is a pure function of a trace, `detect_safety_issues(trace) -> list[SafetyFinding]`, so the same code path runs against stored traces (the batch mode HW4 asks for) and against a live trace before returning to the caller.

*Alternative rejected:*

- **Reimplement Layer 2 + Layer 4 in `planazo.safety`.** Two copies of the same rule; a change to the invariant in one place would silently drift from the other.

## Decision

Use MLflow for tracing + evaluation storage. Wire `langchain.autolog()` at every composition root through a `configure_tracing()` helper that is idempotent and defaults to a `file:./var/mlflow` backend. Hand-roll two `@mlflow.trace` decorators — one on `run_once` (root AGENT span), one on `search_events_rag` (RETRIEVER span). Tag every trace with `request_origin`, `eval_case_id` (when applicable), and `agent_kind` via `mlflow.update_current_trace(tags=...)` from helpers in `src/planazo/observability/tracing.py`. Land the HW4 evaluation as a new `src/planazo/eval/agent/` subpackage (scenarios, models, metrics, adapters, runner, CLI) and expose three console scripts: `planazo-agent-eval` (Part 1), `planazo-trace-scorers` (Part 2), `planazo-safety-batch` (Part 3). Reuse the HW3 scorer bodies verbatim; land Part 3 layers 1 and 3 as new code and cite layers 2 and 4 to the ADRs that already own them.

## Consequences

### Positive

- **The tracing prerequisite Part 1 needed is done first**, so the trajectory-capture step is one small adapter — the exact ordering the assignment recommends.
- **Scorer bodies stay untouched**, so HW3 tests keep passing without modification.
- **One MLflow experiment holds every trace**: production (`request_origin ∈ {bot, cli}`), eval (`request_origin=eval` + `eval_case_id=<case>-run-<k>`), batch scorer passes (`request_origin=batch`) — separated by tag, not by experiment name.
- **The safety detector composes cleanly**: it runs over the same stored traces the scorers do, in the same batch.
- **Docker portability is preserved.** `var/mlflow/` sits under the existing `./var:/app/var` bind mount from ADR 0026 — no new mount needed.

### Negative / accepted trade-offs

- **Token counts are estimates, not usage.** OpenCode Zen does not return `token_usage` in the LangChain response shape. The `estimate_tokens()` helper uses tiktoken as a fallback; the report calls this out explicitly.
- **MLflow's file store is not concurrent-safe** for many writers on the same host. Fine for `docker compose up` scale; would need SQLite or a tracking server for a multi-node deployment.
- **LangChain autolog logs one recoverable warning** on every recommender turn (`ChatMessage` validation for tool-call content parts). It does not block the run — traces still land — but it clutters stdout. Not fixed in this ADR; upstream issue.
- **The eval harness raises temperature to 0.7** (documented) so 3 runs actually vary. The rest of the runtime keeps its 0.0 default.

### Follow-ups

- Land the HW4 submission report (`HW4_SUBMISSION.md`) + a HW4 section in the root `README.md` (matches the HW3 pattern at `README.md:237`).
- Add `MLFLOW_TRACKING_URI` to `.env.example` (optional; default is file-backend).
- If the ChatMessage-validation warning becomes annoying, either filter it in the CLI entrypoints or upstream a fix.
- If the eval fleet outgrows a laptop, reconsider decision 1 in favor of a hosted tracking server.
