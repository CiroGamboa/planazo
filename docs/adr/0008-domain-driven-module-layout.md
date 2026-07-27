# 0008 — Domain-driven module layout for `planazo/`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** cirogam22

## Context

The Planazo agent runtime has grown correctly through ADRs 0001–0004 + 0007 — no import cycles, memory/query/monitor are already well-encapsulated packages — but the domain is organized around *boundary type* rather than *aggregate*:

- `agent/src/planazo/schemas/events.py` holds calendar-tool boundary models AND `SearchIntent` in the same file (documented in `AGENTS.md` as intentional).
- `agent/src/planazo/schemas/domain.py` (101 LOC) mixes four aggregates: `Event`+`ExtractionRunIndexEntry`, `UserRecord`+`PreferenceRecord`, `ApprovalDecision`.
- `agent/src/planazo/storage/dao.py` (411 LOC, the largest module in the tree) is a flat file — each function stays within its aggregate but the module itself doesn't say so.
- `agent/src/planazo/agents/event_agent.py::run_once` imports across every context (9 non-agentlib modules) as the sole composition root; there is no application-service layer.
- `agent/src/tools/tools.py`'s calendar reference tools bypass the DAO and persist JSON directly, so "event candidate" (JSON) and "event" (SQLite) are two parallel persistence paths.
- `agent/src/planazo/agents/loop.py` is 100% generic (no domain knowledge, imports only `agentlib.tools`) but sits inside a package named for domain agents.
- `Event.extra: dict[str, object]` is a schemaless escape hatch on an otherwise typed domain entity — a future Instagram source (M2) will need to name what fields belong here.

The open milestones M2 (Instagram source), M3 (Extraction Agent), M4 (Recommender + Ranker), M5 (Telegram bot), M6 (`/find` handler wiring) will each introduce new domain entities named in `AGENTS.md`'s Data Contracts table (`RawEventCandidate`, `ExtractionError`, `RankedEventList`, `CalendarDraft`). Landing them into the current shape would deepen the "boundary-type grouping" pattern; landing them into a domain-driven layout lets each aggregate arrive in its own home.

Alternatives considered:

- **Big-bang refactor** — move everything at once in a single large PR. Rejected: too much blast radius, would collide with in-flight tickets (M2/M3 assume `save_event` name-locked by ADR 0003), and hard to review.
- **Defer entirely** — leave the current layout until M6 is done, then reorganize. Rejected: every new aggregate lands in the wrong home first, doubling the eventual move cost. The user's ask is now.
- **Repositories-only, no per-context packages** — refactor `dao.py` into per-aggregate repository classes but leave the file layout flat. Rejected: half-measure that solves the DAO size problem without addressing the "boundary-type vs aggregate" grouping. Aggregate models still stuck in `schemas/domain.py`.
- **Aggregates under `planazo/schemas/` (each aggregate = one file)** — keep the `schemas/` package as the home for all aggregate models. Rejected: schemas/ is currently a "boundary validation" package by ADR intent; folding aggregates in there muddles the role. A dedicated per-context folder that co-locates models + repository + tools is the DDD-standard shape and matches how `memory/` and `query/` are already organized.

## Decision

Planazo adopts a **domain-driven module layout** under `agent/src/planazo/`, one folder per bounded context, each carrying its own models + repository + (where applicable) LLM tool adapters. Contexts sit alongside the existing shared kernel (`agentlib/`, `tools/schema.py`) and the existing agent runtime + application layer (`planazo/agents/`).

**Target tree:**

```
agent/src/
├── agentlib/                    (shared kernel — LLM wrapper; ADR 0001)
├── tools/                       (shared kernel — schema_for reflection; ADR 0001)
└── planazo/
    ├── catalog/                 → Event, ExtractionRunIndexEntry, EventRepository,
    │                              save_event/search_events tool adapters
    ├── identity/                → UserRecord, PreferenceRecord,
    │                              UserRepository, PreferenceRepository
    ├── approval/                → ApprovalDecision, ApprovalRepository, ApprovalGate
    ├── calendar/                → EventCandidateInput, CalendarConfirmationInput,
    │                              CandidateStore, calendar tool adapters
    ├── query/                   → SearchIntent, interpret()      (already here)
    ├── memory/                  → Fact, Note, MemoryScopeRequest,
    │                              memory-tool closures            (already here)
    ├── monitor/                 → RunStep, Verdict, judge, service (already here)
    ├── discovery/               → RawEventCandidate, ExtractionError,
    │                              source adapters, Extraction Agent (reserved for M2/M3)
    ├── recommendation/          → RankedEventList, ranker         (reserved for M4)
    ├── storage/                 → db.py (connection + migrations only)
    └── agents/
        ├── loop.py              → generic runtime (unchanged this cycle)
        ├── event_agent.py       → composition root (unchanged this cycle)
        └── cli.py               → CLI surface
```

**Naming convention across contexts:**

- `<context>/models.py` — Pydantic aggregates + value objects.
- `<context>/repository.py` — data-access primitives, connection-parameterized; each function stays within its aggregate. No ORMs; no cross-aggregate joins in a single method.
- `<context>/tools.py` — LLM tool adapters (flat-scalar wrappers + typed error dicts). Only present when the context exposes tools to the agent loop.
- `<context>/__init__.py` — re-exports the public API used by other contexts.

**What this ADR preserves (contract-locked from earlier ADRs):**

- **Public tool names.** `save_event` and `search_events` (ADR 0003) keep their names and call signatures. `save_event_candidate` and `confirm_and_create_calendar_event` (ADR 0002) keep their names. The four memory tool names bound by `build_memory_tools(user_id)` (ADR 0004) keep their names. Every downstream milestone (M2 imports `save_event`, M3 imports `dispatch_extraction`, M6 imports `interpret`, etc.) reaches these functions by unchanged name; only the import path changes.
- **The `user_id` closure discipline** (ADR 0004) — memory tools bind `user_id` via nested closure, never as a parameter. A refactor may not turn `user_id` into a keyword arg.
- **The `ApprovalGate` protocol** — the callable interface (`approve(tool_name, arguments) -> bool`) used by CLI + monitor + future bot stays identical. `ApprovalGate` relocates from `planazo.agents.loop` to `planazo.approval.gate`; every current caller updates its import.
- **The `RunStep` JSONL contract** (ADR 0007) — the wire schema for `data/runs/*.jsonl` and `agent/var/extraction_runs.jsonl` is untouched. The monitor's `on_step` hook binding survives.
- **The two-tier persistence pattern** in `storage/dao.py` — connection-parameterized primitives that let `sqlite3.IntegrityError` propagate, plus flat-scalar wrappers that own their own connections and return typed dicts. Each context's `repository.py` inherits this pattern.

**What this ADR supplements (not supersedes):**

- ADR 0001 pinned `agent/src/agentlib/` and `agent/src/tools/schema.py` outside `planazo/` as product-agnostic infrastructure. This ADR **preserves that pin** — no runtime code moves out of the shared kernel. New bounded contexts sit under `planazo/`.
- ADR 0003's `save_event` / `search_events` names + signatures — preserved intact, only import path changes.
- ADR 0004's memory + `user_id` closure discipline — preserved intact, `memory/` folder does not move.

**What this ADR defers to follow-up work (out of scope):**

- **Moving `planazo/agents/loop.py`** to a `planazo/platform/` runtime package. `loop.py` has zero domain knowledge and is a natural fit for the shared kernel, but the move touches every current importer (`event_agent`, `cli`, `monitor/logging`) and would need to supersede ADR 0001's layout claim for the kernel. Deferred to a follow-up ADR + milestone.
- **Refactoring `event_agent.run_once` to take an `ApplicationServices` container** instead of importing every context directly. The right shape depends on which services M4 (ranker), M6 (`/find` composer), M7 (bot handlers) actually need; premature to design against speculation. Deferred until M4 + M6 have landed.
- **Tightening `Event.extra: dict[str, object]`** into per-source typed payloads. Requires M2's Instagram source to know what fields real sources produce.
- **Consolidating the `agent/src/tools/` naming smell** (package + `tools.py` inside it, mixing pure infrastructure with reference tools). The reference tools move into `planazo/calendar/` in this refactor; a follow-up can decide whether `agent/src/tools/` becomes purely `schema.py`'s home.

**Incremental adoption path:**

The refactor lands as one milestone with 5 tickets (see the milestone "Domain refactor — aggregates + repositories"). Each ticket moves one bounded context, updates every importer via mechanical import-path replacement, and keeps the full test suite green. Tickets are independent — any order, any subset — but the recommended sequence starts with the smallest (approval, then identity, then catalog, then calendar; monitor/cli hygiene runs first as a small warm-up).

## Consequences

### Positive

- **Every new aggregate lands in its own home** — `RawEventCandidate` (M2), `ExtractionError` (M2), `RankedEventList` (M4), `CalendarDraft` (v0.2) each go into their bounded context on arrival, not into `schemas/domain.py`.
- **Repositories are testable in isolation** — a test that only cares about `EventRepository` imports one module, not the 411-LOC `dao.py`.
- **The composition root shrinks** — `event_agent.run_once` will eventually import one thing per context (`catalog.tools`, `identity.tools`, `memory.api`, ...) instead of 9 modules across every layer, once the follow-up `ApplicationServices` refactor lands.
- **Cross-context private-symbol reach becomes visibly wrong** — the current `monitor/cli.py` reach into `agents/cli.py._MISSING_KEY_MESSAGE` becomes a linting smell rather than a "well, they're neighbours" acceptable coupling.
- **Rule 7 (edit-over-create) survives** — we're not creating a parallel universe; each context replaces its share of the existing files, and the old locations are deleted in the same commit (rule 8).
- **Documentation matches implementation** — the bounded-contexts diagram in `docs/MVP-ARCHITECTURE.md` will reflect actual folder shape rather than aspirational grouping.

### Negative / accepted trade-offs

- **Blast radius per ticket is moderate** — each per-context move touches ~5 files (new package + shrunk `schemas/domain.py` + shrunk `storage/dao.py` + `event_agent.py` imports + tests' imports). Not painful, but not free either.
- **Follow-up work is real work**, not just a rename — the `ApplicationServices` container refactor and the loop→`platform/` move are separately-scoped, and this ADR names them without committing to a timeline.
- **`agent/src/tools/` still has the naming duplication** (package `tools/` contains `tools.py`) until a follow-up decides how to split. After this refactor, `tools/tools.py` will be gone (moved into `planazo/calendar/tools.py`), leaving just `tools/schema.py` in the shared kernel — arguably cleaner, but the folder name stays inherited from ADR 0001.
- **Test files carry ~10-line import-path diffs** per ticket. Reviewer discipline: verify the diffs are import-only, no test logic changes.
- **Two contexts (`discovery/`, `recommendation/`) sit empty until M2/M4 populate them.** This is deliberate scaffolding — an empty `__init__.py` in the seed commit tells M2/M4's planners where entities belong without requiring them to argue for a folder in their own planning cycle.

### Follow-ups

- **Milestone: "Domain refactor — aggregates + repositories"** — 5 tickets, one per context (catalog / identity / approval / calendar / monitor-cli-hygiene). Each ticket is independent and keeps the test suite green.
- **Follow-up ADR: runtime kernel consolidation** — supersede this ADR + ADR 0001's layout claim to move `planazo/agents/loop.py` and possibly `agent/src/tools/schema.py` into a shared `planazo/platform/` or similar.
- **Follow-up ADR: `ApplicationServices` composition** — after M4 + M6 land, decide the shape of the composition-root refactor.
- **Follow-up ticket: `Event.extra` typing** — after M2 lands, define per-source typed payload types.
- **In-flight milestone alerts** — the milestones M2, M3, M4 will have their tickets commented with a pointer to this ADR so their planners land entities in the right context.
