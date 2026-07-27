# 0002 — Event-tool boundary, persistence store, and approval-gate policy

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** dvetencourt

## Context

With the runtime decided (see [`0001`](0001-agent-runtime-layout-and-provider.md)), the agent needed its first two tools — real ones, per `docs/PLANAZO-PROJECT-CONTEXT.md`'s requirement that at least one tool "call a real API or persist data," and per `AGENTS.md` rule 1's requirement that every LLM tool-call payload be validated at the boundary before it reaches persisted state.

The project context suggests a longer tool list (`search_eventbrite_events`, `search_meetup_events`, `extract_events_from_instagram_link`, `normalize_event`, `rank_events`, `create_calendar_event_draft`, `confirm_and_create_calendar_event`), but none of Eventbrite, Meetup, Instagram, or Google Calendar credentials exist yet — `.env.example` lists them commented out, unconfigured. Building against any of them now would mean either shipping unreachable code or committing to a specific provider/OAuth integration before it has its own ADR (`AGENTS.md` rule 6 requires one per event-source integration).

Alternatives considered:

- **Wire a real external API call now** (e.g. Eventbrite search, or Google Calendar via OAuth) to make "real" mean "network call." Rejected for v1: Eventbrite's public search endpoint no longer serves third-party search without an org-scoped token, and Google Calendar needs an OAuth consent flow with no credentials configured — either would be unreachable in tests and demos, or would force a provider/OAuth decision ahead of its own ADR. The project context explicitly allows "persist data" as the alternative to "call a real API," and a local JSON store is genuinely real: it is read back, re-read to verify writes, and is what downstream tools (ranking, calendar confirmation) actually depend on.
- **A single tool that both saves and confirms.** Rejected: it collapses a reversible action and an irreversible one into one call, which either over-gates the reversible half or under-gates the irreversible half. The approval-gate policy needs the split to have any teeth.
- **`confirm_and_create_calendar_event` takes full event details as fresh arguments**, independent of any prior saved candidate. Rejected: it discards the natural "browse candidates, then confirm one" flow, and gives up a clean way to demonstrate a distinct not-found error branch.

## Decision

Two tools, both under `agent/src/tools/tools.py`, both validated through Pydantic v2 boundary models in `planazo/schemas/events.py` (`AGENTS.md` rule 1):

- **`save_event_candidate`** — reversible. Persists one normalized event candidate to `var/event_candidates.json`. No approval gate: re-saving a corrected candidate is just another write, and nothing downstream is unlocked by calling it. Returns a typed `error_type` (`invalid_event_data` for a malformed date or out-of-range field, `low_confidence_extraction` when the extraction confidence is below `0.3`) instead of persisting an unreliable or malformed record — directly implementing the project context's requirement that unreliable extractions get a typed error state, not a silent success.
- **`confirm_and_create_calendar_event`** — irreversible. Looks up a previously saved candidate by `event_id`, then persists a calendar entry to `var/calendar_events.json`, optionally recording invitee emails to notify. Treated as irreversible per `AGENTS.md` rule 3 even though the JSON row itself could technically be deleted: the effect (a calendar entry visible to the user, invitations visible to third parties) is what matters, not whether our own storage happens to be mutable. Returns a typed `error_type` (`invalid_confirmation_data`, `missing_invitees` when notification is requested with no addresses, `event_not_found` when `event_id` was never saved) rather than creating a broken or partial entry.

**Persistence store:** local JSON files under `agent/var/`, one per tool, following the same read-modify-write-then-verify pattern as SkillPilot's tools (write, then re-read from disk before returning, so the tool's reported result reflects what is actually on disk). SQLite was the project context's other named option; JSON is chosen for v1 because both stores are small, append-only, single-writer collections with no query needs beyond "does this `event_id` exist" — a real database adds ceremony with no present benefit. Revisit if/when concurrent writers or real queries appear.

**Approval-gate policy:** `IRREVERSIBLE_TOOLS = {"confirm_and_create_calendar_event"}`, consumed by `planazo.agents.loop.ApprovalGate` exactly as SkillPilot's loop already supports — the loop itself has no Planazo-specific knowledge; the CLI wires the gate to a terminal `y`/`N` prompt, matching `AGENTS.md` rule 3 ("no persistent 'always allow', no test-only shortcut promoted to prod").

**Tool-boundary contract:** each tool takes flat, LLM-schema-friendly scalar arguments (so `tools.schema.schema_for`'s signature-reflection keeps working unchanged), then constructs a Pydantic model internally to validate and normalize before touching disk. A `ValidationError` (or an unparseable `start_time`) becomes a typed `error_type` result — valid-shaped data the model can read and react to — never an unhandled exception. An exception a tool raises anyway (a bug, a disk error) is caught one layer up, in `run_loop`'s dispatch try/except, and fed back as a `tool_failed` marker — that generic catch, inherited unchanged from SkillPilot, is what turns an actual tool failure into its own branch instead of letting it look like valid data.

## Consequences

### Positive

- Both tools are exercised end-to-end by tests with no external credentials required — the entire test suite (`uv run pytest`) runs with only the LLM mocked, and `confirm_and_create_calendar_event`'s tests cover every named error branch.
- The candidate → confirm relationship models the actual product flow (rank candidates, then confirm one) instead of two unrelated persistence calls.
- The typed-error-state pattern is exercised at two independent layers: tool-owned validation (`error_type` in the return value) and the loop's generic exception catch (`tool_failed` marker) — so a reviewer can see both AGENTS.md rule 4's "no silent success" and the class material's "one place a tool failure reaches the loop as its own branch" satisfied by different, complementary code paths.

### Negative / accepted trade-offs

- Neither tool makes a real network call, so "at least one tool calls a real API" is satisfied via its explicitly-allowed alternative ("persist data") rather than demonstrated live. Revisit once an event-source or Google Calendar integration gets its own ADR.
- `var/*.json` is single-writer, unindexed, and not safe for concurrent processes — fine for a CLI-driven agent, not fine if a bot process and a CLI ever write concurrently. No migration path is defined yet.
- `confirm_and_create_calendar_event`'s "irreversible" framing is a policy decision, not a technical one — the JSON row can be edited or deleted directly on disk. The gate protects against the *agent* creating it without approval, not against direct file tampering.

### Follow-ups

- Real event-source tools (Eventbrite/Meetup search, Instagram extraction) and the real Google Calendar integration each need their own ADR before they replace or extend `save_event_candidate`/`confirm_and_create_calendar_event`, per `AGENTS.md` rule 6.
- If a ranking tool (`rank_events`) is added, it should read from `var/event_candidates.json` the same way `confirm_and_create_calendar_event` does, rather than introducing a second candidate store.
- Revisit the JSON persistence choice if a second concurrent writer (e.g. a bot process) is introduced.
