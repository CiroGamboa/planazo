# ADR 0015: Storage migrations and observability

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** cirogam22
- **Landed by:** M3.6
- **Relates to:** [`0003-sqlite-domain-store.md`](0003-sqlite-domain-store.md) (schema-evolution seam refined), [`0004-three-store-memory-model.md`](0004-three-store-memory-model.md) (rationale is a DB-inside audit surface, not a fourth memory), [`0005-multi-agent-shape.md`](0005-multi-agent-shape.md) (observability writer discipline mirrors the audit-log discipline), [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (`observability/` follows the per-context pattern), [`0012-multi-event-extraction.md`](0012-multi-event-extraction.md) (§Follow-ups filed the migration framework; §Trade-offs named the `schema_v1.sql` rewrite-in-place footgun this ADR closes).

## Context

Three forces converged going into M3.6.

**Silent DDL no-ops kept biting.** `storage/schema_v1.sql` was a single `CREATE TABLE IF NOT EXISTS` script applied on every `db.connect()`. Any schema change (a column addition, a UNIQUE key swap) required deleting `var/planazo.db` on every dev host; forgetting to delete produced `save_event_failed` at runtime with no signal until an operator debugged the FK/column mismatch. #64 first surfaced this on the composite-UNIQUE swap; ADR 0012 §Trade-offs called it out and filed a migration framework as a follow-up. Every schema-touching ticket since (`scan_state` in ADR 0011, `event_index_in_post` in ADR 0012) paid the same tax.

**The `events` domain model was under-shaped.** The M3 baseline held only what `save_event` needed at the tool boundary — no source-account attribution, no venue, no organizer, no tags, no description, no ticket/image URLs, no language, no recurring marker. The Extractor's multimodal turn produces richer data than the schema could absorb; the Recommender's M4 ranker needs those columns to filter on. `Event.category` was a free-form `str` while `SearchIntent.categories` was already a `Literal` — the two aggregates could drift silently and had begun to.

**Loops needed relational audit rows.** M3 shipped two JSONL sidecars (`data/runs/*.jsonl` for the Recommender, `var/extraction_runs.jsonl` for the Extractor) that carry per-tool-call turns for the monitor. Neither is queryable at loop grain — "how many Recommender runs did user A start yesterday" is a JSONL scan, not a `SELECT`. M4's `/find` history reader (#23) and M4's ranker's rationale corpus (#20) both need per-loop rows joined by `run_id` to per-decision rows, with rationale text stored verbatim inside the trust boundary.

Doing all three in one milestone (M3.6) means one migration sequence, one review, one delete-stale-DB event for the entire M3 → M3.6 transition.

## Decision

Planazo lands a versioned, in-transaction migration framework at `src/planazo/storage/`; grows `events` to the full domain model in one migration pass and lifts `Event.category` to the shared `EventCategory` Literal; and introduces a new `observability/` bounded context that persists one `agent_runs` row per completed loop and 0..N `llm_decisions` rows per loop, wired best-effort at every composition root alongside the existing JSONL sidecars. The nine load-bearing choices below encode both the shape and the discipline. The `schema_v1.sql` file is deleted in the same pass; live references get repointed at `src/planazo/storage/migrations/`.

### 1. Migration framework: `PRAGMA user_version` + versioned `src/planazo/storage/migrations/*.sql`, applied in order inside per-file transactions

`storage/db.py::connect()` reads `PRAGMA user_version`, discovers every `NNN_<name>.sql` file under `storage/migrations/` in lexicographic order, and applies each file whose numeric prefix exceeds the current `user_version`. Each apply runs as `BEGIN; <sql>; PRAGMA user_version = <N>; COMMIT;` — a mid-migration failure rolls back both the DDL and the version bump, leaving the database at the last successful version rather than a half-applied one. A `user_version` greater than the newest migration on disk is refused (the code cannot describe a schema written by a future version). The M3.6 sequence ships four files: `001_baseline.sql` (the pre-framework tables reconstituted), `002_events_domain.sql` (the ten new columns + two composite indexes), `003_agent_runs.sql`, `004_llm_decisions.sql`.

**Rejected alternatives:**

- **Keep the `CREATE TABLE IF NOT EXISTS` + operator deletion pattern.** Rejected: this is exactly what caused #64's `save_event_failed`. `IF NOT EXISTS` silently no-ops any DDL against a database that already has the table under an earlier column shape — the operator gets a runtime FK/column mismatch error, not a schema-drift warning. Every schema change under this pattern is a new bug at every developer host that forgot to `rm var/planazo.db`. The `PRAGMA user_version` approach fires loudly at connect time on a schema mismatch, and applies deterministically in the same order on every host.
- **A `schema_versions` table with a row per applied migration.** Rejected in favour of `PRAGMA user_version`: the pragma lives inside SQLite's own metadata (no bootstrapping problem — the pragma is present on a brand-new DB), needs no separate DDL to introduce, and carries the single integer the runner actually reads. A row-per-migration table adds a second surface (its own DDL, its own tests, its own bootstrapping edge case) for no gain at MVP scale.

### 2. Down migrations deferred

The runner only applies forward migrations. There is no reverse SQL alongside each `NNN_<name>.sql` file, no `down` block, no operator command to roll one step back.

**Rejected alternatives:**

- **Land down migrations alongside up.** Rejected for MVP: every schema failure to date has been fix-forward (the failing branch was reverted at the code level, and a follow-up migration corrected any partial write). No production rollback surface exists yet — the CLI is single-host, single-writer. Reversibility becomes a real requirement once we ship to prod; a follow-up ticket adds `down` blocks + a `planazo-migrate --down` command in the same PR as the first prod deploy. Landing them now would test unused code and defer other M3.6 work.

### 3. `observability/` bounded context owns first-order run persistence

A new folder under `src/planazo/observability/` owns `AgentRunRecord`, `LLMDecision`, `format_stored_text`, the `record_agent_run`/`query_agent_runs`/`record_llm_decision`/`query_llm_decisions` repository primitives, and the two best-effort `AgentRunLogger`/`LLMDecisionLogger` writers. Composition roots (`agents/event_agent.py::run_once`, `agents/extractor.py::extract_once`) instantiate the loggers alongside the existing JSONL loggers and hand them the built records at loop completion.

**Rejected alternatives:**

- **Extend `monitor/`.** Rejected: `monitor/` owns out-of-band LLM-as-judge grading (ADR 0007) — a separate clock, a separate CLI, a separate STRONG-tier model call that reads the JSONL sidecars and writes markdown verdicts. Conflating primary run persistence with that context breaks ADR 0008's per-context focus discipline. The monitor consumes what observability produces; the two should not share a folder.
- **Put `agent_runs` in `catalog/`.** Rejected: `catalog/` is domain-facing — it owns `Event`, the shared surface the Recommender and Extractor read/write against, and the tool wrappers (`save_event`, `search_events`) the LLM sees. `agent_runs` is audit-facing — a loop-grain record the LLM never touches, whose readers are the operator (`sqlite3` CLI, `/find` history in M6) and the ranker's rationale corpus (M4). Putting it in `catalog/` would blur the "LLM-facing vs operator-facing" distinction the bounded-context split is there to preserve.

### 4. `agent_runs.stopped` mirrors `LoopResult.stopped` values (minus `preference_read_error`)

`AgentRunRecord.stopped: Literal["answered", "truncated", "max_steps"]`. The DB CHECK excludes `preference_read_error`.

**Rejected alternatives:**

- **Introduce a new observability-specific taxonomy.** Rejected: `LoopResult.stopped` is already the loop's terminal-state vocabulary. Adding a second taxonomy would mean maintaining a mapping every time the loop's set changes, with the extra risk of the two drifting silently. The excluded `preference_read_error` branch fires before any LLM turn — the composition root returns early with no run to record. Recording it would violate the invariant that every `agent_runs` row corresponds to at least one LLM turn, and every reader (M6's `/find` history, M4's ranker corpus) is written against that invariant.

### 5. `llm_decisions.rationale` is DB-inside; not redacted at write, only length-capped + control-char-sanitized

`LLMDecision.rationale: str` is capped at `RATIONALE_CAP = 500` characters and passed through `format_stored_text` (strip C0/C1 control chars + DEL, collapse whitespace, cap length). The full text of the LLM's reasoning is persisted — the sanitizer only strips bytes that would corrupt downstream string readers.

**Rejected alternatives:**

- **Apply the `[error_type: <token>]` redaction from ADR 0012's `_build_result` here too.** Rejected: Rule 2's redaction is a **boundary-crossing** guard for text going from the Extractor's return surface into the Recommender's messages (where the caption text could re-enter a prompt). The DB is inside the trust boundary — the operator, `/find` history projection, and ranker corpus are the readers, none of which are LLM-facing prompt surfaces. Full LLM reasoning is what makes the audit trail useful; redacting it here would leave every future rationale reader with `[error_type: <token>]` and nothing to explain. Redaction happens on the way OUT to any operator-facing or model-visible surface, not on the way IN to the DB.

### 6. Observability writes are best-effort

Every writer (`AgentRunLogger.record`, `LLMDecisionLogger.record_many`) wraps its INSERT sequence in `try / except Exception`, logs a WARNING through the module logger on failure, and swallows the exception. The primary flow (Recommender answer, Extractor `ExtractionResult`) never sees an audit-writer failure. `LLMDecisionLogger.record_many` extends this to per-row: one bad row in a batch does not lose the rest.

**Rejected alternatives:**

- **Propagate writer failures.** Rejected: Rule 4 — a failed audit write must never break the primary flow. A disk-full `mkdir`, a stale schema on a mis-migrated dev DB, an FK violation from a hand-composed test fixture — none of these should make the Extractor's `save_event` chain roll back or the Recommender's answer fail to reach the user. Best-effort keeps the JSONL sidecar as the fallback audit surface: a missing `agent_runs` row degrades observability, not correctness.

### 7. Retention rotation deferred (worry-later)

No cron sweep, no `agent_runs_archive` table, no size cap on the DB file. The DB grows unbounded until a follow-up ticket wires retention.

**Rejected alternatives:**

- **Land rotation now.** Rejected: at expected pre-production scale — ~1 KB per `agent_runs` row × ~240 scheduler-driven runs/day + interactive traffic — the DB grows ~90 MB/year, well inside SQLite's comfort zone on the host filesystem. Adding a retention policy now means designing (what's the horizon? per-user or global? what happens to `llm_decisions` for deleted `agent_runs`? does `event_db_id`'s `ON DELETE SET NULL` matter for retention too?) and testing rotation for a problem that does not exist yet. A follow-up ticket lands rotation when the operator sees growth become real.

### 8. `Event.category: str` → `Literal["tech", "cultural", "music", "networking", "sports", "other"]` (aligned with `SearchIntent.EventCategory`)

The `EventCategory` Literal owned by `query/models.py` now constrains both `SearchIntent.categories` and `Event.category`. A category outside the set is a `ValidationError` at model construction — the repository/tool layer turns it into `invalid_event_data`, matching the discipline every other `save_event` validation follows.

**Rejected alternatives:**

- **Keep `Event.category: str`.** Rejected: `SearchIntent.categories` is already a Literal, and the ranker filters events by category equality against that Literal. A silent drift — the interpreter's model outputting `"technology"` while an Extractor row was saved as `"tech"`, or vice versa — is a bug factory: nothing fires at either write, the row just never matches. Aligning both aggregates on one Literal moves the failure to model-construction time, where Rule 1 catches it as a typed validation error. The compat break for existing dev DBs is accepted per #64's precedent — dev DBs are ephemeral, and the migration framework (Decision 1) makes the next such change safe.

### 9. Events table grows to the full domain model in one migration pass

`002_events_domain.sql` adds ten columns to `events`: `source_account`, `venue_name`, `venue_address`, `organizer`, `tags` (JSON-encoded array in TEXT), `description`, `ticket_url`, `image_url`, `language`, `recurring` (0/1 INTEGER). Two composite indexes ship with the same migration: `idx_events_city_start(city, start_utc)` and `idx_events_category_start(category, start_utc)` back the two hot Recommender filter shapes.

**Rejected alternatives:**

- **Land columns as separate tickets.** Rejected: every added column would be a separate migration file, a separate PR, a separate reviewer round, and — under the migration framework — a separate `user_version` bump but no separate delete-stale-DB event (the framework makes additive columns transparent). Bundling them means one review of the full domain shape, one alignment check against what the Extractor's multimodal turn produces, and one migration sequence to reason about. Nothing about the ten columns is independent — they land or don't-land together as "the full domain model the Recommender's M4 ranker filters on."

## Consequences

### Positive

- **Schema changes are safe by construction.** The migration framework applies in a transaction, bumps `user_version` inside the same commit, and refuses to open a DB written by a future version of the code. `#64`'s silent-no-op class of bug is gone. Additive columns need no delete-stale-DB event; a breaking change is loud at connect time on every host.
- **Every completed loop is a queryable row.** `SELECT COUNT(*) FROM agent_runs WHERE user_id = ? AND started_at > ?` is one SQL statement. The JSONL sidecars still carry per-tool-call grain for the monitor, but the loop-grain question stops being a JSONL scan.
- **Rationale corpus is available for M4.** `llm_decisions.rationale` stores the full LLM reasoning per terminal decision. M4's ranker can read the corpus without re-running any extraction; M6's `/find` history projects rationale-with-redaction on the operator-facing surface.
- **The `events` domain model matches what the LLM produces.** Venue, organizer, tags, description, ticket/image URLs, language, and the recurring marker are all persisted. The Recommender's M4 ranker has the columns to filter on, and category alignment closes the interpreter-vs-catalog drift channel.
- **Observability discipline is code-shape enforced, not prompt-enforced.** Composition roots hand the loggers already-validated `AgentRunRecord` / `LLMDecision` objects. `format_stored_text` sanitizes at construction and the model boundary re-checks — a caller that bypasses the helper fails at `AgentRunRecord.__init__`, not at DB write.

### Negative / accepted trade-offs

- **One compat break for existing dev DBs on the M3 → M3.6 transition.** `Event.category` moves from `str` to a Literal (Decision 8); any pre-M3.6 dev DB with a non-Literal category value fails Pydantic validation on read. Operator action: `rm var/planazo.db` once at the branch-checkout event. Precedent: #64 accepted the same shape.
- **No rollback surface yet.** Down migrations (Decision 2) are deferred. A schema mistake needs a fix-forward migration, not a reversal. Acceptable for pre-production; a follow-up wires the reverse arm.
- **Unbounded DB growth until retention lands.** Retention rotation (Decision 7) is worry-later. Every completed loop writes ~1 KB to `agent_runs` + up to ~5 KB across `llm_decisions`; growth is bounded by human interaction rate and scheduler cadence.
- **Duality of JSONL + SQLite audit surfaces.** Both the JSONL sidecars (per-tool-call grain, consumed by the monitor) and the new SQLite tables (loop-grain + decision-grain, consumed by future readers) co-exist. Deleting the JSONL surface is out of M3.6 scope; a future convergence ticket decides whether one supersedes the other.
- **`tags` filtering has no index.** `json_each` + `WHERE tag = ?` works but no index on JSON contents means a scan. Acceptable at expected scale (<10k rows in year 1); a follow-up materializes a normalized `event_tags` table if scan cost becomes real.

### Follow-ups

- **Retention rotation ticket** — file when the operator sees real growth (~year-1 metric).
- **Down migrations + `planazo-migrate --down`** — land alongside the first production deploy.
- **JSONL → SQLite convergence** — decide whether the per-tool-call JSONL sidecars retire once the SQLite tables prove themselves in prod.
- **M4 ranker corpus reader** (#20) — reads `llm_decisions` grouped by `decision_kind`.
- **M6 `/find` history reader** (#23) — reads `agent_runs` joined to `llm_decisions` with rationale redaction on the operator-facing projection.
- **Normalized `event_tags` table** — only if JSON-array tag filtering becomes a hot path.

## Related

- [`0003 — SQLite + JSON columns for the domain store`](0003-sqlite-domain-store.md) — this ADR refines the schema-evolution seam that 0003 left open (`§Follow-ups`: "A schema-version table lands with the first actual v2 schema change"). Decision 1 lands the version-tracking primitive using `PRAGMA user_version` in place of a `schema_versions` table.
- [`0004 — Three-store memory model`](0004-three-store-memory-model.md) — `llm_decisions.rationale` is a fourth memory concern (a persisted DB-inside audit surface), but it stays inside SQLite alongside the domain store rather than joining the JSON docstore or the markdown rules. It is not a fourth backend.
- [`0005 — Multi-agent shape`](0005-multi-agent-shape.md) — the observability writer discipline (best-effort, module-logger WARNINGs, per-row error isolation for batch writes) matches the audit-log discipline ADR 0005 established for the Extractor's JSONL sidecar.
- [`0008 — Domain-driven module layout`](0008-domain-driven-module-layout.md) — `observability/` follows the per-context folder pattern (`models.py`, `repository.py`, best-effort writer alongside), preserving the "one bounded context, one folder, one aggregate family" rule.
- [`0012 — Multi-event extraction`](0012-multi-event-extraction.md) — §Follow-ups filed the migration framework; §Negative trade-offs named `schema_v1.sql`'s rewrite-in-place footgun. This ADR closes both.
