# 0001 — Agent runtime layout, LLM provider, and orchestration approach

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** dvetencourt

## Context

Planazo had a product spec (`docs/PLANAZO-PROJECT-CONTEXT.md`) and a rulebook (`AGENTS.md`) but no code yet. A companion project, SkillPilot, had already built and validated — in its own repo — an observe-reason-act-verify agent loop, an LLM-provider wrapper, and a tool-schema-derivation utility, all hand-rolled per the same "no agent frameworks in v1" constraint this repo's `AGENTS.md` rule 5 also states. Rather than re-derive that plumbing from scratch, we needed to decide how much of it to bring over, where it lives, and what changes.

SkillPilot's version lived under `backend/` alongside a FastAPI app and a Next.js `frontend/`, because SkillPilot is a multi-surface product (learner-facing quiz UI, course dashboard). Planazo's first version has no such surface — `AGENTS.md` already anticipates a Telegram bot as the eventual user-facing entrypoint, not a REST API or a web frontend — so porting the FastAPI app and the `backend`/`frontend` split made no sense here.

Alternatives considered:

- **Copy `backend/` wholesale, including FastAPI.** Rejected: nothing in Planazo's v1 scope calls an HTTP endpoint, and an unused FastAPI app is a maintenance and audit surface for no benefit.
- **Put the agent code directly under `src/` at the repo root.** Considered, but the rest of the repo (docs, ADRs, `.claude/`) is language-agnostic, and a future bot process, worker, or second runtime would want its own sibling directory anyway — better to establish that boundary now than to retrofit it.
- **Re-derive the loop/provider wrapper from scratch, independently.** Rejected: it would re-litigate design decisions (stopping conditions, approval-gate contract, failure-marker shape) that SkillPilot already made and validated live against the real provider, with no reason to diverge.

## Decision

**Layout:** a single `agent/` directory at the repo root — no `backend/`/`frontend/` split, no FastAPI, no server. `agent/src/agentlib/` (the OpenCode Zen wrapper) and `agent/src/tools/schema.py` (the `schema_for()` reflection utility that derives a tool's JSON schema from its function signature and docstring) are ported unchanged from SkillPilot — they are product-agnostic infrastructure. `agent/src/planazo/agents/` (the loop, the tool binding, the CLI) and `agent/src/tools/tools.py` (the two event-discovery tools) are Planazo-specific and are new code, not a port, even though `loop.py`'s generic dispatch mechanics are carried over unchanged.

**Provider:** OpenCode Zen (`https://opencode.ai/zen/v1`, OpenAI-compatible, Responses API), accessed through the OpenAI Python SDK, exactly as SkillPilot validated it. Two pinned model roles — `cheap` and `strong` — cover every call; all provider access stays isolated behind `agentlib`, so a future provider swap is a contained change there, not a rewrite of `planazo.agents`.

**Orchestration:** a hand-rolled loop, not a framework — `planazo.agents.loop.run_loop` calls the model with provider-native tool schemas, dispatches whatever tool calls come back through a caller-supplied registry, and feeds results back until the model answers or a step cap is hit. This satisfies `AGENTS.md` rule 5 directly.

**Toolchain:** `uv` for dependency management, `ruff` for lint+format, `mypy --strict` for types, `pytest` for tests — matching the root `AGENTS.md`'s "Setup & commands" section, now scoped to `agent/` instead of the repo root.

## Consequences

### Positive

- No unused FastAPI/frontend surface to maintain, secure, or explain to a future contributor.
- The provider integration is already proven (SkillPilot made live calls against both model tiers before this decision), not theoretical.
- `agentlib` and `tools/schema.py` are verbatim ports — zero behavioral drift from validated code, and a future upstream fix to either is a mechanical re-copy.
- The loop's genericity (SkillPilot enforced this with a test that greps `loop.py`'s own source for tool-domain literals — kept here) means Planazo's actual tool set is a caller-side concern, not a loop change.

### Negative / accepted trade-offs

- Two independent copies of `agentlib`/`schema_for` now exist (SkillPilot's and Planazo's), with no shared package — a fix to one does not propagate to the other. Acceptable for now since both are small, stable, and vendored deliberately; extracting a shared internal package is a future decision, not a v1 requirement.
- No FastAPI/HTTP surface means no way to drive the agent except the CLI until the Telegram bot (or another surface) lands — acceptable since v1 scope is the agent loop itself, not a deployed surface.
- OpenCode Zen's model catalog and pricing are undated and undocumented; `MODELS`/`PRICES` in `agentlib.core` are a manually pinned snapshot inherited as-is, with no automatic drift detection.

### Follow-ups

- See [`0002-event-tool-contracts-and-approval-gate.md`](0002-event-tool-contracts-and-approval-gate.md) for the tool boundary, persistence store, and approval-gate policy built on top of this runtime.
- A Telegram bot (or other user-facing surface) entrypoint is out of scope here; when it lands, it should call the agent loop the same way the CLI does, through `planazo.agents.event_agent.run_once`.
- If a second runtime (e.g. a worker) is ever added, revisit whether `agentlib`/`tools/schema.py` should move to a shared package rather than staying vendored per-runtime.
