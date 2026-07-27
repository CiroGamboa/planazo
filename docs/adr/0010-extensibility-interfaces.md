# 0010 — Extensibility interfaces at the four swap seams

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Relates to:** [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (bounded contexts), [`0009-repo-root-layout.md`](0009-repo-root-layout.md) (flat root layout), [`0001-agent-runtime-layout-and-provider.md`](0001-agent-runtime-layout-and-provider.md) (runtime + provider).

## Context

The owner named four axes the system must be ready to change without a rewrite: **user surface** (Telegram → WhatsApp / web frontend / …), **data sources** (Instagram → TikTok / YouTube / news pages / Meetup / Eventbrite), **database** (SQLite → Postgres eventually), **agent loop** (hand-rolled → LangChain / LangGraph / anything else). None of these swaps is planned for v1; the scope is *interface readiness*, not migration.

ADR 0008's bounded-context refactor already put each swap seam at a folder boundary — `planazo.bot/` for the surface, `planazo.sources/` for adapters, per-context `repository.py` modules for persistence, `planazo.agents.loop` for the runtime kernel. What ADR 0008 did not do is *name* those seams as formal interfaces. Today they are conventional: a repository is "connection-parameterized functions matching this shape", the loop is "whatever `run_loop` accepts", a source adapter is "TBD in M2". A conscientious reader can grep and figure it out; a new adapter writer has to reverse-engineer the shape from an existing implementation.

The refactor also produced one preview of a better pattern: `ApprovalGate` in `src/planazo/agents/loop.py` is now a `typing.Protocol` declared inline in the runtime, with the concrete `@dataclass(frozen=True)` in `src/planazo/approval/gate.py` satisfying it structurally. Zero domain imports in the runtime, callers construct the concrete, any structurally-compatible object works — a WhatsApp surface's approve callback conforms without importing the domain class. That pattern already answered a real problem (removing the last `from planazo.` import from `loop.py`). This ADR generalizes it.

Alternatives considered:

- **Do nothing; wait until a swap is imminent.** Rejected. Every future adapter writer (M2's Instagram source, M4's ranker consuming repositories, a future Postgres migration) invents its own understanding of "what this seam expects". By the time the first swap arrives, the seam has drifted into whatever the current single implementation happens to do — the abstraction is retroactive, and it costs more.
- **Abstract Base Classes (`abc.ABC` + `@abstractmethod`).** The Python-alternative to Protocols. Considered. Rejected because ABCs require *nominal* subclassing — a WhatsApp adapter, a CI fake, or a Postgres repository would all need to explicitly inherit from the base class. Protocols are *structural*: any object with the right attributes/methods satisfies them, no import needed. Structural typing matches how Python code actually gets reused across boundaries.
- **Interfaces per-context (each bounded context defines its own protocols).** Considered. Rejected as fragmentation for its own sake. The four seams identified here are cross-cutting: the source-adapter protocol lives at the boundary between `sources/` and `agents/` (whoever consumes a source), not inside one context. Formalizing them in one place (`src/planazo/interfaces.py` or `src/platform/`) keeps the pattern discoverable.
- **Ship a generalized dependency-injection container** (`inject`, `wired`, a hand-rolled service locator). Rejected. The four seams don't need runtime injection today — they're compile-time / import-time swap points. `event_agent.run_once` composes them by direct import; adding a container would introduce indirection nothing yet needs.

## Decision

Formalize the four swap seams with **Python `Protocol` classes**, one per axis. Each Protocol names the shape the runtime (or any consumer) needs from that axis, without importing any concrete implementation. Concrete implementations live in their owning bounded context (or, for the runtime kernel, in `src/planazo/agents/loop.py`).

The four Protocols land in `src/planazo/interfaces/` as one module per seam:

- **`interfaces/surface.py` — `UserSurface`** protocol. Describes what any user-facing surface (Telegram bot, WhatsApp bot, web frontend, CLI) must provide to the agent runtime: a way to receive a user message + a way to reply + a way to request approval (yielding an `ApprovalGate`-compatible callback). Terminal `planazo-agent`'s CLI + the future Telegram bot both conform.
- **`interfaces/sources.py` — `EventSource`** protocol. Describes what any data-source adapter must provide: `fetch_post(url) -> RawPost | ErrorState` + optional `search(intent) -> list[Event]` + a `cadence` accessor for the scheduler. Instagram (M2), future TikTok, YouTube, news, Meetup, and Eventbrite adapters all conform. Details in M2 (#16) + planned ADR 0006.
- **`interfaces/persistence.py` — `Repository[T]`** protocol (generic in `T`, the aggregate type). Describes what a per-aggregate repository must provide: connection-parameterized primitives (`insert`, `query`, `by_id`, …) with typed error branches. SQLite implementations already conform (verified by the type checker after this ADR lands); a future Postgres port swaps the underlying SQL without changing the protocol.
- **`interfaces/runtime.py` — `AgentLoop`** protocol. Describes what the runtime kernel (`run_loop` today) must provide to callers: `run(user_message, tools, registry, ...) -> LoopResult`. A future LangChain / LangGraph adapter provides an implementation of this Protocol; the composition root (`event_agent.run_once`) is unaffected.

**Structural-typing discipline (from the `ApprovalGate` preview):**

- Protocols carry the same name as the canonical concrete class where possible (`ApprovalGate`, `EventSource`). Callers construct from the concrete; consumers accept the Protocol. Python's structural typing means a concrete class doesn't need to `import` its Protocol — it just has to have the shape.
- The Protocol module holds **zero `from planazo.` imports** except for pure data types (Pydantic models, aggregate types). Protocols must not reach into runtime or domain code.
- One `Protocol` per file — the module docstring names the swap axis and the reference implementation(s). Discoverable via `ls src/planazo/interfaces/`.

**What lands in this cycle** (this ADR's own PR): the ADR file itself + `src/planazo/interfaces/` with **stubs** — one file per axis, each declaring the Protocol shape based on today's concrete implementations. No consumers are re-typed against the Protocol in this cycle; that lands piecewise as each downstream ticket touches its seam:

- M2 (#16 Instagram) will type its `sources/instagram/InstagramSource` against `interfaces.sources.EventSource`.
- M4's ranker will type its repository dependencies against `interfaces.persistence.Repository`.
- M5's Telegram bot will type its surface against `interfaces.surface.UserSurface`.
- A future ADR — runtime-kernel consolidation, also flagged by ADRs 0008 and 0009 — will type callers against `interfaces.runtime.AgentLoop`.

Each of those tickets carries a small type-only diff for its own seam. The interface stubs land now so the shapes are agreed *before* any of the downstream tickets picks its concrete details.

## Consequences

### Positive

- **New adapters have a spec, not a scavenger hunt.** M2's Instagram-source planner reads `interfaces/sources.py` and knows exactly what the adapter must expose. A future TikTok source writer does the same. A CI fake does the same.
- **`mypy --strict` catches the drift.** A repository whose signature drifts from `Repository[Event]` fails type-check at the consumer. Silent contract slippage becomes noisy.
- **Zero-cost today, real value on the first swap.** The Protocols are static-typing constructs — no runtime overhead, no dependency injection framework, no import cycles.
- **Domain-agnostic runtime survives.** `agents/loop.py` already has zero `from planazo.` imports (post-ADR-0009); adding `interfaces/runtime.py` as an *external* interface preserves that. When runtime-kernel consolidation eventually moves `loop.py`, it moves against a stable Protocol.
- **Preview validated the shape.** `ApprovalGate` shipped as Protocol + concrete in the M9 refactor; it works cleanly, no cargo-cult overhead.

### Negative / accepted trade-offs

- **Discipline required, not enforced.** Nothing prevents a future implementation from bypassing the Protocol and depending on a concrete. `mypy --strict` catches signature drift but not "I imported the wrong type here". The pattern is a convention; reviewer discipline maintains it.
- **Generic types (`Repository[T]`) require some care.** Python's `Protocol` + generics work but the ergonomics are worse than a plain ABC. Living cost; accepted.
- **Four Protocols land empty of consumers.** Until M2/M4/M5 wire their concrete-vs-Protocol typing, the interface stubs are documentation. Deliberate: land the shapes now so downstream tickets don't have to argue about them.

### Follow-ups

- **M2 (#16) types `InstagramSource` against `interfaces.sources.EventSource`.** No other work — the ticket ships the concrete; the type annotation makes the Protocol conformance explicit.
- **M4's ranker and repository callsites type against `interfaces.persistence.Repository`.**
- **M5's Telegram bot types its surface against `interfaces.surface.UserSurface`.**
- **Future ADR — runtime-kernel consolidation.** Moves `agents/loop.py` under a shared kernel package, types the loop against `interfaces.runtime.AgentLoop`, and renames the inner `agents/` folder (still misleadingly plural today) into `runtime/` (`loop.py`) + `app/` (`event_agent.py` + `cli.py`). Blocked on nothing; deferred for scope hygiene.
- **`ApprovalGate`'s Protocol declaration** moves out of `agents/loop.py` and into `interfaces/surface.py` (approval is a surface concern). The `loop.py` inline declaration made sense as a preview when no interfaces module existed; once it does, the seam belongs there. Small cleanup, follow-up ticket.
