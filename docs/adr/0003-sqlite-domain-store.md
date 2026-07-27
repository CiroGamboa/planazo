# 0003 — SQLite + JSON columns for the domain store

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** dvetencourt

## Context

Planazo's only persistence so far is the two ADR-0002 JSON files (`var/event_candidates.json`,
`var/calendar_events.json`), each a single-writer, unindexed, append-only list with no query
surface beyond "does this `event_id` exist." The MVP (`docs/MVP-ARCHITECTURE.md` §7) needs a real
domain store shared by two agents (Recommender, and the Extractor landing in a follow-up ticket)
and, eventually, a Telegram bot process: `events` (the shared surface both agents read/write),
`users` (the multi-user seam), `preferences` (structured filters pushed into agent context),
`approvals` (audit trail for the approval gate), and `extraction_runs_index` (a thin pointer into
the Extractor's JSONL run log). None of these have a query shape ADR 0002's JSON files can serve
— "give me tonight's tech events in Barcelona" needs a filter, not a linear scan of a list stored
under one process's `var/`.

Alternatives considered:

- **Extend the ADR-0002 JSON files with more of them (one per new table).** Rejected: it does not
  solve the actual problem (no query surface, no relations, single-writer-only), it just adds more
  files with the same limitations, and ADR 0002 itself flagged this as a "revisit if a second
  concurrent writer or real queries appear" trade-off — that moment is now.
- **A full ORM (SQLAlchemy) over SQLite or Postgres.** Rejected: `AGENTS.md` rule 5 keeps this repo
  framework-free at the agent-loop layer; the same instinct applies to persistence — a narrow,
  hand-written DAO over the stdlib `sqlite3` module is enough for five small tables with no
  migrations beyond additive schema growth, and keeps the whole storage layer auditable in one
  file. Postgres specifically is also rejected: it needs a running server process, which this
  single-process CLI/bot repo has no deployment story for yet.
- **Migrate `save_event_candidate`/`confirm_and_create_calendar_event` onto the new `events` table.**
  Rejected: the candidate/confirmation shape (`event_id: str` as an opaque caller-chosen handle, a
  separate unrelated "confirmed calendar entry" concept with no table anywhere in this schema) does
  not map onto `events`' shared-domain shape (`source_url` as the natural key, one row per real
  event). Forcing the fit would corrupt the shared `events` table with a second, incompatible
  writer contract right as the Extractor ticket is about to become its other writer.

## Decision

**Store:** SQLite at `agent/var/planazo.db` (stdlib `sqlite3`, no new dependency), one connection
per call for the tool-facing functions (mirroring ADR 0002's already-established "open, act, close"
tool shape), an explicit shared connection for anything composing multiple calls (push-context
assembly, tests). `:memory:` is a first-class target — every **primitive** dao function takes a
`sqlite3.Connection` as an explicit argument so tests can build one in-memory database, populate
it, and exercise multiple dao calls against the same connection without touching disk. The two
tool-facing wrappers named below (`save_event`, `search_events`) deliberately do not: a
`sqlite3.Connection` cannot be an LLM tool argument, so they open and close their own.

**Schema (v1):** exactly the five tables in `docs/MVP-ARCHITECTURE.md` §7 — `events`, `users`,
`preferences`, `approvals`, `extraction_runs_index` — defined in
`agent/src/planazo/storage/schema_v1.sql` and applied by `storage/db.py::connect()` via
`executescript` on every connection open. Every `CREATE TABLE` is `IF NOT EXISTS`, so applying the
same script twice (two processes, two test runs) is a no-op the second time — "idempotent
migration" means exactly this for v1, with no version-tracking table yet (nothing has changed the
schema since v1; a version table is a real requirement only once a v2 diff exists). `events.extra`
is a `TEXT` column holding a JSON-encoded object — SQLite has no native JSON column type; "JSON
columns via JSON1" means a `TEXT` column plus the JSON1 SQL functions if a query ever needs to look
inside it, not a distinct storage type. Every Pydantic row model (`Event`, `UserRecord`,
`PreferenceRecord`, `ApprovalDecision`, `ExtractionRunIndexEntry`, in
`planazo/schemas/domain.py`) is what a `ValidationError` at the dao boundary turns into an
`invalid_event_data`-style typed error rather than a partial row (`AGENTS.md` rule 1).

**Dao shape — two tiers, not one:** `storage/dao.py` exports (a) connection-parameterized
primitives (`insert_event`, `query_events`, `get_or_create_user`, `get_preferences`,
`set_preference`, `record_approval`, `list_approvals`, `record_extraction_run`,
`list_extraction_runs`) for internal composition and `:memory:`-backed tests, and (b) two
self-contained, flat-scalar-argument functions — `save_event(...)` and `search_events(...)` — that
open their own connection via `db.connect()`, call the matching primitive, and return a
JSON-serializable typed-error-or-success dict, exactly like ADR 0002's tools. This split exists
because a raw `sqlite3.Connection` cannot be an LLM tool argument: `save_event`/`search_events` are
the actual cross-ticket contract this ADR fixes — `search_events` is a pull tool in *this* ticket's
Recommender registry, and `save_event` is what the Extraction Agent ticket imports as its own write
tool into the shared `events` table. Both names are fixed now so neither ticket blocks on the
other.

**Error semantics at the two tiers are deliberately different.** The primitives let
`sqlite3.IntegrityError` propagate — a `set_preference` or `record_approval` call naming a
`user_id` with no `users` row is a caller bug (no LLM tool reaches these; only our own composition
code and tests do), and a loud failure is the correct branch for a bug. The `save_event` wrapper,
which *is* LLM-reachable, converts every failure into a typed error state per `AGENTS.md` rule 4:
`invalid_event_data` for an unparseable timestamp or a `ValidationError`, and **`duplicate_event`
for a `source_url` that already has a row**, carrying the existing `event_db_id` so the caller
learns the row exists rather than just that the write failed. Re-processing a URL is the Extraction
Agent's ordinary case, not an exceptional one, so it needs a named branch. An upsert (overwrite the
existing row) was rejected for this: it would let a later low-confidence re-extraction silently
clobber a better earlier row, which is exactly the "silent success" rule 4 forbids — the caller
should decide whether to replace, not the storage layer.

**Old JSON tools:** `save_event_candidate`/`confirm_and_create_calendar_event` are **not** migrated
onto this schema (see Context — the shapes do not fit) and are **not** deleted. They stay wired in
`tools/tools.py`, JSON-backed exactly as ADR 0002 left them, but they are **opt-in** rather than
default: `event_agent.run_once` includes them only when passed `calendar_enabled=True` (a new
run-context key, default `False`), and `planazo-agent` exposes that as a `--calendar` flag. This is
the MVP architecture doc's own resolution (§Layers, "Recommender executor") — kept as the calendar
reference implementation until v0.2's real Google Calendar wiring replaces them.

They stay *reachable* on the shipped CLI, rather than being hidden entirely, for one specific
reason: `confirm_and_create_calendar_event` is the only irreversible tool in the tree, so it is the
only end-to-end demonstration of `AGENTS.md` rule 3's approval gate that exists. Removing it from
every default path would leave `ApprovalGate` gating a tool no shipped surface can dispatch, and
would silently break the live gate tests (`agent/tests/test_agents_gate_live.py`) that are rule 3's
only proof against a real model. Reachable-but-opt-in keeps the guarantee testable without exposing
a calendar action to an ordinary user before v0.2.

## Consequences

### Positive

- One real query surface (`search_events`) exists where before there was only a linear JSON scan,
  with room to grow (date range, city, category) without a schema change — `extra` absorbs
  source-specific fields the Extractor ticket needs without a migration.
- `:memory:` tests exercise the exact same code path production uses (`storage/dao.py` functions
  against a real `sqlite3.Connection`), not a mock of the database.
- `save_event`/`search_events` are usable as LLM tools immediately (flat scalar args, no connection
  object leaks into a tool schema) — the Extractor ticket can import `save_event` and register it
  in its own `TOOL_REGISTRY` without knowing anything about `storage/db.py`.
- The two ADR-0002 tools keep every existing test green untouched; nothing about their behavior
  changes, only their default visibility to the loop.

### Negative / accepted trade-offs

- No schema-version tracking table yet. Fine for v1 (one schema, no diffs to apply), but the first
  actual v2 change needs to add one rather than keep hand-rolling `IF NOT EXISTS` forever.
- `approvals` and `extraction_runs_index` get a tested dao layer (round-trip tests) but no
  production caller in this PR — their first real writer is the approval-gate-on-Telegram ticket
  and the Extraction Agent ticket respectively. This is the same "infrastructure lands ahead of its
  consumer, with its own tests" pattern ADR 0001 already accepted for `agentlib`/`tools/schema.py`.
- `geo_lat`/`geo_lng` on the flat `save_event` tool signature default to `0.0` (matching the
  existing repo convention of a sentinel default over `Optional` on LLM-tool-facing flat
  parameters, e.g. `confirm_and_create_calendar_event`'s `invitee_emails: str = ""`) rather than a
  true "unknown" state. Acceptable for Barcelona-only v1; revisit if geo precision ever matters.
- Single SQLite file, single process. Fine for the CLI and the eventual single-instance Telegram
  bot process (`docs/MVP-ARCHITECTURE.md` explicitly rules out concurrent workers for v1); would
  need revisiting before any horizontal scaling.

### Follow-ups

- The Extraction Agent ticket wires `save_event` into its own tool registry and starts writing real
  rows the Recommender's `search_events` reads back — the shared-memory coordination point named in
  `docs/MVP-ARCHITECTURE.md` §Multi-agent coordination.
- A schema-version table lands with the first actual v2 schema change, not preemptively now.
- Revisit the ADR-0002 tools' JSON persistence (and whether `calendar_enabled` ever flips to `True`
  by default) when the real Google Calendar OAuth integration lands in v0.2.
