# ADR 0020 — Catalog curator admin agent

**Status:** Accepted
**Date:** 2026-07-29
**Related:** [ADR 0003 (SQLite domain store)](0003-sqlite-domain-store.md), [ADR 0005 (multi-agent shape)](0005-multi-agent-shape.md), [ADR 0008 (domain-driven module layout)](0008-domain-driven-module-layout.md), [ADR 0011 (scheduled ingestion)](0011-scheduled-ingestion.md), [ADR 0015 (storage migrations + observability)](0015-storage-migrations-and-observability.md), [ADR 0017 (Instagram demo narrative logs)](0017-instagram-demo-narrative-logs.md).

## Context

The `events` table is append-only today. The Extractor `INSERT`s rows from Instagram posts and no code ever `DELETE`s or `UPDATE`s them. Three problems accumulate as the catalog grows:

1. **Stale events.** The Recommender happily serves events whose `end_utc` is in the past. Grep for `expired` / `archive` / `stale` / `is_past` across `src/planazo/` returned zero implemented hits pre-milestone; only an aspirational note in `observability/models.py` mentioned a "future retention sweep".
2. **Duplicates.** The same event announced by two accounts (venue + promoter). The `UNIQUE(source_url, event_index_in_post)` constraint only blocks re-extraction of the same URL, not cross-account dupes. No `find_duplicates` primitive existed.
3. **Mis-classified categories.** The Extractor's LLM picks an `EventCategory` per event, sometimes wrong (a networking event tagged `music`, a music show tagged `cultural`, etc.). No correction path existed.

The Recommender and Extractor deliberately don't have write access to arbitrary `events` rows — the Recommender is read-only against the catalog, and the Extractor only INSERTs. What we need is a **new admin-scoped agent** that can prune and correct existing rows, on its own clock, with a full audit trail.

## Decision

We ship a new bounded context `src/planazo/curator/` — a peer of `scheduler/`, `observability/`, `conversation/` — that runs a STRONG-tier LLM loop on a daily cron. The agent has six tools (three read, three write) and operates through the same `agentlib.run_loop` seam the Recommender and Extractor use.

### D1: Soft delete via `events.archived_at`, not physical DELETE

**Decision:** Migration 008 adds `events.archived_at TEXT NULL`. `NULL` means the event is live; a non-NULL ISO-8601 timestamp means the curator (or an operator) retired it. `query_events` + `get_event_by_id` default to hiding archived rows via `WHERE archived_at IS NULL`; `include_archived=True` is the admin opt-out for the curator's own tools and the monitor.

**Rejected alternative:** physical `DELETE FROM events`. **Reason:** LLM decisions on production data must be reversible. A mistaken archive is one `UPDATE events SET archived_at = NULL WHERE id = <id>` from being undone; a mistaken DELETE requires a DB backup.

### D2: LLM decides, tools enforce

**Decision:** The curator is an LLM agent — the six curator tools give it read access to actionable slices and write access to soft-delete / merge / update-category primitives. The LLM picks which rows to archive, which duplicate to keep, and which category is correct.

**Rejected alternative:** rules-only heuristics (e.g., "auto-archive anything with `end_utc < now - 1 day`"). **Reason:** the "which of two duplicates is canonical?" decision needs judgment on title similarity, venue name specificity, and source-account authority that hard-coded rules can't safely make. The "is this category wrong?" decision is even more judgment-heavy. Fixed heuristics work for the stale-event case; they don't scale to the harder two.

### D3: STRONG tier, `max_steps=12`

**Decision:** Curator uses `agentlib.core.STRONG` (`gpt-5.4-strong`, ~$0.02-0.05 per tick) with `max_steps=12` and `max_output_tokens=2000`.

**Rejected alternative:** CHEAP tier (`gpt-5.4-nano`). **Reason:** curator mistakes cost catalog quality — an incorrectly-archived popular event is much worse than an incorrectly-recommended one (the recommender's next turn recovers; the archive persists until the operator notices). The extra ~$0.03/tick is worth it. Daily cadence caps this at ~$0.90/month, well within MVP budget.

### D4: Singleton state row (`curator_state`)

**Decision:** Migration 009 creates `curator_state` with `CHECK (id = 1)` — one row, ever. Carries `last_run_at`, `last_success_at`, `consecutive_failures`, and three lifetime mutation counters (`total_archived`, `total_merged`, `total_categories_fixed`). Seeded with `INSERT OR IGNORE INTO curator_state (id) VALUES (1)` so a fresh DB reads back a defaults-only row.

**Rejected alternative:** per-tick state files (`var/curator_state.json` overwritten per tick, or a growing log of state snapshots). **Reason:** the singleton row mirrors `scan_state`'s scheduler pattern and integrates cleanly with future rotation. Reads and writes are atomic (single-row transaction) with no filesystem coordination.

### D5: Daily cron at 03:00 UTC, separate from `planazo-scheduler`

**Decision:** New CLI `planazo-curator --tick` with its own crontab entry. Not folded into `planazo-scheduler --tick`.

**Rejected alternative:** fold into the scheduler tick. **Reason:** different cost profile (STRONG once per tick vs. STRONG per URL), different failure mode (bad LLM decisions on shared state vs. bad extractions on new rows), and different observability need (categorical audit-log vs. per-URL ingestion log). A separate cron entry, a separate audit log (`var/curator_runs.jsonl`), and a separate CLI keep the two concerns cleanly separated.

### D6: New `'curator'` value in `AgentName` Literal + `agent_runs.agent_kind` CHECK

**Decision:** Migration 010 extends `agent_runs.agent_kind CHECK` from `('recommender', 'extractor')` to `('recommender', 'extractor', 'curator')` via SQLite's standard rebuild-and-copy pattern (no `ALTER CONSTRAINT`). Mirroring `observability/models.py::AgentName` and `monitor/models.py::AgentName` Literals extend to include `'curator'`. `llm_decisions.decision_kind` CHECK is extended in the same migration to add `'archive'`, `'merge'`, `'update_category'`.

**Rejected alternative:** keep the enum locked at two agent kinds and use a generic `'admin'` bucket for any future admin agent. **Reason:** every future admin agent (broadcaster, operator dashboard, source health steward) will have distinct observability needs — the monitor's per-agent filter, the future dashboard's per-agent aggregations. A distinct `agent_kind` per agent keeps those queries clean. The enum stays closed, but grows deliberately.

### D7: `dry_run` is a composition-time knob, not an LLM-facing tool arg

**Decision:** `build_curator_tools(dry_run=bool)` returns the six-tool registry with `dry_run` closed over the three write tools. When `dry_run=True`, write tools return `{"status": "dry_run", ...}` after validating input; no DB mutation happens. The LLM never sees `dry_run` as a parameter.

**Rejected alternative:** expose `dry_run` as an LLM tool parameter. **Reason:** the LLM should not decide whether the tick is a dry run. That's an operator-level choice made at CLI invocation time. Baking it into the tool factory keeps the LLM's tool schemas honest — every call the LLM issues, it means to execute.

### D8: Best-effort observability writers (Rule 4)

**Decision:** `AgentRunLogger`, `LLMDecisionLogger`, `curator_state` upsert, and `var/curator_runs.jsonl` append all swallow exceptions and log a WARNING. The tick's primary flow — the DB mutations the curator tools already committed — is unaffected.

**Rejected alternative:** propagate writer failures. **Reason:** matches Rule 4 discipline established by ADR 0015 for the recommender + extractor. Audit failures must never invalidate primary work.

### D9: Rationale is DB-inside per Rule 2

**Decision:** The LLM's `reason` argument to a write tool is captured in `llm_decisions.rationale` after `format_stored_text` sanitization. Full LLM reasoning is allowed subject to the 500-char cap and control-char stripping. The stdout `--verbose` narrative log strictly excludes `reason` — only ids, counts, and Literal-valued fields cross that boundary.

**Rejected alternative:** redact `reason` at the DB boundary too. **Reason:** matches the ADR 0015 rationale hook — DB-inside is the trust boundary; redaction happens on the way OUT (to a user-facing surface), not on the way in. Keeps the audit trail useful for post-hoc "why did the curator archive this?" inspection.

## Consequences

- One new bounded context (`curator/`), one new CLI script (`planazo-curator`), three new migrations (008, 009, 010).
- The Recommender's read surface (`query_events`, `search_events`) transparently hides archived rows. Every read caller gets the same result set they did before the curator existed, minus any rows the curator has retired.
- The monitor (`planazo-monitor`) picks up curator runs automatically once `AgentName` includes `'curator'` — no monitor changes needed for M3.7-scope M7 use.
- Cost: ~$0.90/month at daily cadence with STRONG tier.

## Follow-ups (deliberately out of scope)

- **Approval gate on write tools.** The curator's write tools currently auto-execute. A follow-up could wire `src/planazo/approval/gate.py::ApprovalGate` for `archive_event` / `merge_events` if operator wants human-in-the-loop safety.
- **Fuzzy duplicate detection.** `list_duplicate_candidates` groups by exact `normalized(title) + date + venue`. Levenshtein or embedding similarity is a future ticket.
- **Operator daily-summary DM.** Currently the operator has to check `var/curator_runs.jsonl` or the audit log. A future ticket could DM the operator via Telegram.
- **Rotation / retention on curator-archived rows.** Soft-deleted rows accumulate forever. A future retention sweep can hard-delete rows older than N days.
