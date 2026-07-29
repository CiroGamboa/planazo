# ADR 0016: Multi-turn Recommender conversation

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** cirogam22
- **Landed by:** M3.7 T2
- **Relates to:** [`0004-three-store-memory-model.md`](0004-three-store-memory-model.md) (preferences remain the profile surface — no new memory tier), [`0005-multi-agent-shape.md`](0005-multi-agent-shape.md) (the Recommender/Extractor split still defines the loop shape), [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (`conversation/` follows the per-bounded-context layout), [`0011-telegram-bot-interface.md`](0011-telegram-bot-interface.md) (the bot layer holds no LLM; the service composition root does), [`0013-recommender-mutation-and-clarification-boundaries.md`](0013-recommender-mutation-and-clarification-boundaries.md) (the `ask_user` tool + `ClarificationRequest` shape this ADR persists), [`0015-storage-migrations-and-observability.md`](0015-storage-migrations-and-observability.md) (`agent_runs.run_id` is the join key for the follow-up patterns).

## Context

Three forces converged going into M3.7 T2.

**Clarification is one-way today.** `ClarificationRequest`, the Recommender's `ask_user` tool, and `RecommenderResult.status = "needs_clarification"` all exist and pass validation in the tree. The CLI (`planazo-agent`) simply prints the question and exits; there is no way for a caller to hand the user's answer back into the same intent shape and re-run the loop. The result is that a vague `/find` never converges — the user just retries with more specific text and the system learns nothing.

**Bot restarts erase in-flight state.** The bot (`planazo.bot`) is single-instance, long-polling on a laptop, and restarts on every code change during development. Keeping "which clarification did we ask" in a Python dict would drop the state on every restart — the user's next message would be treated as a fresh query, ignoring the question we just asked. On MVP scale, this fragility means a demo works only when nothing crashes.

**"More results" and "tell me about #N" both need per-run memory.** The M3.6 `recommendations` table persists 0..N rows per completed loop, joined by `agent_runs.run_id`. That's the substrate for the two follow-up patterns the plan promises: rank-position lookup for "tell me about the second one" and exclusion filtering for "show me more". Both require the calling surface to remember `run_id` between turns — the Recommender itself is stateless per invocation.

Doing all three (persisting clarification round-trips, remembering the last run for follow-ups, enriching the profile with clarification answers) in one ticket means one migration, one review, one composition root that transports (Telegram bot today; a future CLI helper; the web UI when it arrives) share.

## Decision

Planazo lands the multi-turn conversation as a new `conversation/` bounded context: one migration (`006_conversation_state.sql`) for the per-user scratchpad, one Pydantic aggregate cluster (`ConversationState` + `PendingClarification` + `ConversationReply`), one connection-parameterized repository, and one service composition root (`handle_user_message`) that transports call. The five load-bearing choices below encode both the shape and the discipline. `conversation/` sits above `query/`, `identity/`, `observability/`, `catalog/`, and `agents/event_agent`; it composes their primitives rather than reimplementing them (Rule 8).

### 1. DB-backed conversation state, not in-memory

The scratchpad lives in a new `conversation_state` table — one row per user, keyed on `user_id`, upserted every message. `pending_clarification` and `last_recommendation_run_id` are the two mutable columns; `updated_at` is stamped on every write for future operator queries.

**Rejected alternatives:**

- **Keep the state in a Python dict on the bot process.** Rejected: the bot restarts on every code change during development, and even the production single-instance shape restarts on a config reload. Losing "we just asked which category" mid-conversation is worse than the demo failure mode of "the user's next message is treated as fresh" — it's a silent misfire the user cannot diagnose. DB-backed state costs one migration and roundtrips cleanly through the existing `db.connect()` seam.
- **Store the state as a `PreferenceRecord` row under a `state:*` key namespace.** Rejected: `PreferenceRecord.value` is capped at 200 characters and enforces a single-line invariant (see `identity/models.py`). A serialised `PendingClarification` carries a full `SearchIntent` — several hundred bytes of JSON with newlines the sanitizer would either strip or reject. Preferences are the profile-enrichment surface (Decision 2 below), not the state-machine surface.

### 2. Clarification answers land as preference-namespaced enrichment rows

When the user answers a clarification, the service writes exactly one `PreferenceRecord` under a `pref:clarified.<derived_key>` namespace (`pref:clarified.categories` for a "which category" question, `pref:clarified.city` for "which city", falling back to `pref:clarified.general` when the question does not match a known phrasing). The next Recommender loop picks the row up as push context via the existing `_preferences_text` path — no new prompt-injection surface, no new store.

**Rejected alternatives:**

- **Land a new `user_profile` table for the enriched fields.** Rejected: the push-context assembly (`event_agent._preferences_text`) already reads `preferences` rows for its bounded system-message text. Adding a second table means a second reader, a second migration, and a second sanitisation surface — for a value that fits the existing row shape (single line, ≤200 chars). The namespaced key + `pref:clarified.` prefix makes the provenance greppable without a new relational structure.
- **Fold the clarification answer straight into the next `SearchIntent`'s `categories` tuple.** Rejected: `SearchIntent` is the interpreter's output, not the profile. Mutating an intent based on a stored answer would either duplicate the interpret step (running it twice per turn) or break the "the interpreter's tool call is the only source of truth for one intent" invariant. Storing the answer as a preference lets the next `interpret()` call see it as context and produce a naturally-shaped intent.

### 3. Per-user single row, not per-thread

`conversation_state.user_id` is the PRIMARY KEY. A user has exactly one row at any moment; a second concurrent conversation from the same Telegram account would overwrite in place.

**Rejected alternatives:**

- **Key on `(user_id, chat_id)` so a user in two Telegram groups has two separate conversations.** Rejected for MVP: Telegram delivers `/find` from a single user in a single chat almost always (DMs), and the bot's `resolve_user` seam maps `telegram_user_id` (not chat_id) into the internal identity. A future ticket that adds group-chat support can widen the key with a migration — but MVP has no way to reach that shape.
- **Key on `(user_id, thread_id)` where thread_id is the bot's own token.** Rejected: adding thread_ids means the bot has to mint and remember them, the user has to see them somewhere in the reply, and every message has to carry them. All of that infrastructure for a use case no MVP user has hit is over-fitting to a future concern.

### 4. "More results" filters client-side, does not mutate `SearchIntent`

When the user asks for more results, the service re-runs `run_once` with the same `SearchIntent` (rebuilt from the prior run's `agent_runs.user_query` JSON) and filters the returned candidates against the set of `event_id`s already persisted under `recommendations WHERE run_id = <prior>`. No new field on `SearchIntent`, no reach into the M4 typed-recommender-executor code, no new tool contract.

**Rejected alternatives:**

- **Add `SearchIntent.excluded_event_ids: tuple[int, ...]` and let `run_once` honour it.** Rejected: the M4 `SearchIntent` + `run_once` shape landed on `main` in parallel with this ticket, and touching it means coordinating with the colleague-side branch. The MVP shortcut of filtering client-side is a strict subset of the eventual push-down: the same set of exclusions, applied one layer higher. A follow-up ticket can push the filter down when the intent shape stabilises.
- **Store the shown event IDs on the `conversation_state` row.** Rejected: the shown set is already persisted as `recommendations` rows FK'd to `run_id`. Duplicating it into `conversation_state` would violate rule 8 — one source of truth per fact. Reading it back through `query_recommendations(run_id=...)` is a two-column indexed lookup (`idx_recommendations_run_rank`), cheap enough for the hot path.

### 5. One shared `interpret + run_once` seam; no CLI-vs-bot fork

The bot's `/find` handler, any future CLI helper, and the tests all reach the multi-turn logic through `conversation.service.handle_user_message`. There is no bot-specific branching around the Recommender; the CLI and the bot share the same composition root.

**Rejected alternatives:**

- **Keep the bot's `/find` on a hand-rolled `interpret + run_once` pair and layer state-tracking on top.** Rejected: the bot would grow its own state-machine code, the CLI would keep its stateless path, and the two would drift on every new feature. The service seam is what makes `handle_user_message` testable offline against a recording surface and re-usable when the second transport arrives.
- **Fold `handle_user_message` into `event_agent.run_once` itself.** Rejected: `run_once` is the Recommender loop. Adding state-tracking + follow-up patterns + preference-enrichment there would mix orchestration concerns with the loop's own composition. The `conversation/` context keeps orchestration one layer up so the loop stays focused.

## Consequences

### Positive

- One migration, one review, one composition root — every transport that wants the multi-turn shape reaches it through `handle_user_message`.
- `pending_clarification` roundtrips through the bot's restart cycle: a user who answers "music" 30 seconds after a bot code reload still gets the enriched search.
- The `recommendations` table earns its keep — the two follow-up patterns (`tell me about #N`, `more results`) are one query each against the existing composite index.
- Clarification answers become durable profile data, so `/find`'s effectiveness compounds turn over turn without a new store or a new push-context surface.

### Negative / accepted trade-offs

- The service imports across five other bounded contexts (`query`, `identity`, `observability`, `catalog`, `agents.event_agent`). This is intentional for a composition root, but it means a rewrite of the service touches five neighbouring test suites' composition assumptions. The per-context tests still run independently.
- "More results" filters client-side; a Recommender run that surfaces 100 candidates just to have 99 filtered out is wasted budget. The tradeoff is worth it for MVP — a follow-up ticket can push the filter down when `SearchIntent` stabilises.
- The `pref:clarified.*` namespace is a magic-string convention, not a first-class schema. Operators reading `preferences` rows see the prefix and know the provenance, but there is no CHECK constraint enforcing it. A future migration can lift the discipline into the DB.
- The service reads `agent_runs.user_query` as the intent JSON to rebuild the intent for "more results". If the audit write was disabled (`record_runs=False`) or a Rule 4 best-effort swallow suppressed it, "more results" degrades to a fresh interpret. That's a legitimate branch — the reply is still useful — but it's a silent narrowing the operator can only see via the logs.

### Follow-ups

- Push the exclusion filter down to `SearchIntent.excluded_event_ids` when the colleague-side M4 branch stabilises; keep client-side filtering as the fallback for backward compatibility during rollout.
- Widen the primary key to `(user_id, chat_id)` when group-chat support lands.
- Land a periodic retention sweep that drops `conversation_state` rows for users inactive > N days.
- Consider a `CHECK` constraint on `preferences.key` enforcing the `pref:*` namespace shape when we cross 100 keys.
- Land a `/find history` view (issue #23) that reads the same `recommendations` + `agent_runs` rows this ADR uses.
