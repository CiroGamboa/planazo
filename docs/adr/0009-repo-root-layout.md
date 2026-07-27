# 0009 — Repository root layout (flatten `agent/`)

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Supersedes:** [`0001-agent-runtime-layout-and-provider.md`](0001-agent-runtime-layout-and-provider.md)'s **Layout** paragraph only. That ADR's provider and orchestration decisions remain in force; only the "single `agent/` directory at the repo root" claim is retired.

## Context

ADR 0001 introduced a single `agent/` directory at the repo root as a foil to SkillPilot's `backend/`/`frontend/` split, on the reasoning that "a future Telegram bot process, worker, or second runtime would want its own sibling directory anyway — better to establish that boundary now than to retrofit it." Under that plan the tree grew four levels deep to reach the domain code:

```
planazo/            (repo root)
└── agent/          (product-runtime directory)
    └── src/        (Python src layout)
        └── planazo/  (the Python package — same name as the repo)
            └── agents/  (a bounded context)
```

Two problems surfaced as the tree filled in:

- **The outer `agent/` is redundant.** No second runtime has arrived; the Telegram bot (M5) will land under `planazo.bot/` as a bounded context, not a sibling process directory. `agent/` is holding one thing (the package + its tests + its scripts) and adds a nesting level with no discriminating power. A reader hits `planazo/agent/src/planazo/` and asks the honest question the ADR 0001 rationale never quite answered.
- **The domain refactor (ADR 0008) sharpened the ask.** With bounded contexts now organized under `planazo/`, the runtime is the *domain* — there is no other product surface for `agent/` to be a peer of. Keeping the outer directory pretends at a split that does not exist.

Alternatives considered:

- **Keep `agent/` on the possibility a second runtime might arrive.** Rejected: ADR 0008 defined `planazo.bot/`, `planazo.monitor/` (already landed), and future adapters (M2 `sources/`) as *contexts within the same package*, not sibling processes. The premise for the outer directory has been superseded by a different architectural direction, not just deferred.
- **Flat layout — package at repo root (no `src/`).** Considered. Common enough in the Python ecosystem (Django, Flask, Requests) and it does look cleanest at a glance, but it collides visibly with the repo name (`planazo/planazo/` at the top of every path) and it forfeits the src-layout guardrail against accidentally importing a working-tree copy that shadows the installed package. That guardrail is a soft benefit today (single package, no rich CLI harness) but it is free and the community-standard default for library-shaped projects. Rejected as more churn for less clarity.
- **Rename the inner `agents/` folder at the same time.** Considered. `planazo/agents/` holds `loop.py` (generic runtime), `event_agent.py` (composition root), `cli.py` (surface) — none of which are "agents" in a domain sense; a DDD-purer name would be `runtime/` or `app/`. Rejected for this ADR: the rename touches every importer and every test and is orthogonal to the outer-layout change. A separate follow-up ticket (deferred, see Consequences) will address it under the same runtime-kernel-consolidation umbrella ADR 0008 already flagged.

## Decision

Delete the outer `agent/` directory. Every file it held moves up one level:

- `agent/src/planazo/` → `src/planazo/` (the package tree — bounded contexts within it are unchanged).
- `agent/src/agentlib/` → `src/agentlib/` (the LLM-provider wrapper — shared-kernel location unchanged relative to `src/`).
- `agent/src/tools/` → `src/tools/` (shared-kernel `schema_for` reflection + the calendar reference tools — location unchanged relative to `src/`).
- `agent/tests/` → `tests/`.
- `agent/scripts/` → `scripts/`.
- `agent/data/` → `data/` (committed markdown rules).
- `agent/var/` → `var/` (gitignored runtime state).
- `agent/pyproject.toml` → `pyproject.toml`, `agent/uv.lock` → `uv.lock`, `agent/.python-version` → `.python-version`.
- `agent/README.md` → `README-package.md` at root (renamed to distinguish from the top-level README, which stays as the repo overview).

Every path referenced in `pyproject.toml` is already relative and stays valid once the file lives at root (`packages = ["src/planazo", "src/agentlib", "src/tools"]`, `[tool.mypy] files = ["src"]`, `[tool.pytest.ini_options] testpaths = ["tests"]`).

Documentation is rewritten in the same commit (AGENTS.md §Setup, `docs/MVP-ARCHITECTURE.md`, `README.md`, the moved `README-package.md`) so it reads as if the new layout is the only state (rule 10). ADRs 0001–0008 are immutable historical records — they are not edited; this ADR is what a reader consulting 0001's layout paragraph will be redirected to.

**What this ADR preserves.** No import path changes. `planazo.catalog.save_event` is still `planazo.catalog.save_event`; the package name and every module path within it are unchanged. Every cross-ticket API contract preserved by ADRs 0002/0003/0004 (public tool names, memory-tool closure discipline, approval-gate signature, `IRREVERSIBLE_TOOLS`) stays byte-for-byte intact. Both console scripts (`planazo-agent`, `planazo-monitor`) resolve to the same entry points via `pyproject.toml` — only the file that declares them is at a different path on disk.

**What this ADR explicitly does not change.** The inner `planazo/agents/` folder keeps its name in this move. Renaming it to something clearer (`runtime/`, `app/`) is a separate refactor that is left to the runtime-kernel-consolidation follow-up ADR 0008 already committed to.

## Consequences

### Positive

- **Three levels to the domain code instead of four.** `src/planazo/catalog/models.py` reads naturally where `agent/src/planazo/catalog/models.py` demanded a second look.
- **`cd agent` disappears from every workflow.** `uv run pytest`, `uv run ruff check`, `uv run planazo-agent` all run from the repo root, matching what any Python contributor expects on landing.
- **The repo's shape now matches its purpose.** A domain-driven-designed package sits under `src/`, tests next to it, docs next to those. No mysterious outer directory that once meant "the backend of a two-surface product".
- **New bounded contexts have a shorter home path.** M2's `planazo/sources/`, M5's `planazo/bot/`, M4's `planazo/recommendation/` all read as one level under `src/planazo/` rather than three.

### Negative / accepted trade-offs

- **Every doc that referenced `agent/src/...` needs a rewrite** in the same commit. Rule 10 (docs describe current state only) is the discipline — `AGENTS.md`, `MVP-ARCHITECTURE.md`, `README.md`, the moved `README-package.md`, and any downstream docs get updated together; ADRs 0001–0008 stay unchanged and remain immutable historical records.
- **A future-me reading ADR 0001 will need to notice this supersede.** Standard cost of the "supersede via new ADR" pattern.
- **The inner `agents/` folder is still misleadingly named** (it houses the generic runtime + composition root + CLI, not domain agents). Accepted for this ADR; slated for the runtime-kernel-consolidation follow-up.

### Follow-ups

- **Rename `planazo/agents/`** to `planazo/runtime/` (for `loop.py`) + `planazo/app/` (for `event_agent.py` + `cli.py`) as part of the runtime-kernel-consolidation ADR 0008 already deferred.
- **Migrate `planazo/schemas/`** — the last vestige of the boundary-type grouping — into per-context `models.py` files. `planazo.schemas.events` (calendar boundary + `SearchIntent`) splits into `planazo.calendar.models` + `planazo.query.models` when the calendar refactor eventually revives; `planazo.schemas.memory` (`Fact`, `Note`, `MemoryScopeRequest`) moves into `planazo.memory.models`. Each is a small chore.
- **Consider a `bin/` or `scripts/` reorganization** once M5 adds a bot entrypoint alongside the existing CLI + monitor.
