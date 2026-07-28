# 0013 — Registration conversation state lives on the `users` row

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** system-architect-planner (planning #56)
- **Relates to:** [`0003-sqlite-domain-store.md`](0003-sqlite-domain-store.md) (the schema this supplies the first real v2 change for), [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (`identity/` owns `UserRecord`), [`0011-telegram-bot-interface.md`](0011-telegram-bot-interface.md) (no session table; create-on-first-contact), [`../MVP-ARCHITECTURE.md`](../MVP-ARCHITECTURE.md#7-storage--srcplanazostorage).

## Context

#56 asks for a guided, multi-turn registration conversation (display name, age, location, language, nationality) that persists into the `identity` bounded context, updates in place on re-run, and — the part that has no precedent yet in this codebase — is **resumable**: if a user answers two of five steps and then goes quiet, their next message (of any kind) continues at the third step rather than restarting. #57 (free-text routed to the agent loop) and #58 (per-user FIFO queue) both land after this ticket in the same milestone and both need to ask "is this user's next message a registration answer, or something else?" before they decide what to do with it. Whatever answers that question here is the mechanism they build on, which is what makes it a decision worth writing down rather than an implementation detail local to one file.

Three things are already fixed by earlier ADRs and by the ticket itself:

- **No session table.** ADR 0011 deliberately keyed the bot's whole notion of "session" to `telegram_user_id` → one `users` row, create-on-first-contact, and rejected a session table as unnecessary machinery. Reopening that decision for registration alone — adding a table whose only job is "which step is this user on" — would fork session state into two places for no operational gain, since it is still exactly one row per user either way.
- **`preferences` is the wrong home for identity fields.** #56's own notes flag two concrete reasons: `PreferenceRecord.value` is capped at 200 characters and rejects a line break (fine for `city, techno` style filters, not guaranteed for free-text location or nationality), and every preference row is unconditionally rendered into the agent loop's system message by `event_agent._preferences_text` on every run. Age, location, language, and nationality are identity facts the ranker will *eventually* read (M4, explicitly out of scope here) — they are not ranking inputs today, and putting them in `preferences` would push them into every model call's context whether or not that was ever intended.
- **No schema-version tracking table exists yet.** ADR 0003 shipped `schema_v1.sql` applied via idempotent `CREATE TABLE IF NOT EXISTS` and named its own gap explicitly: "the first actual v2 schema change needs to add one [a version table] rather than keep hand-rolling `IF NOT EXISTS` forever." This ticket is that first v2 change.

**Alternatives considered.**

- **A dedicated `registration_progress` table** (`user_id`, `pending_field`), separate from `users`. Rejected: it is a 1:1 child of `users` carrying a single nullable column, which buys no relational benefit — every read of a user's state would need a join or a second query for information that describes the same row. It also re-opens the "second session concept" problem ADR 0011 already closed.
- **A new `user_profiles` aggregate**, mirroring the `preferences` shape but keyed by five typed columns instead of one JSON blob. Rejected for the same 1:1-child reason, and because `UserRecord` already holds `display_name` — one of the five fields — so a second aggregate would split "the same person's name" across two tables depending on which of two nearly-identical tickets wrote it.
- **In-memory conversation state** (a module-level `dict[telegram_user_id, step]` inside the bot process). Rejected: it does not survive a process restart, which directly contradicts "resumable" as a durability property, and it would be invisible to #58's queue, which needs to reason about a user's state from the same connection the handler already opens rather than from process memory a queue worker might not share.
- **Deriving "next unanswered step" from which profile columns are `NULL`**, with no separate progress marker at all. Attractive because it needs no extra column, but breaks on the very first step: `users.display_name` is already non-`NULL` for every user via create-on-first-contact (§`bot/session.py`), so a null-column scan would always read the display-name step as already answered and skip it. An explicit pointer is required once any step's target column can be non-null before that step has actually run.

## Decision

**The five profile fields land as five new nullable columns on `users`, and one more nullable column (`pending_registration_field`) is the entire conversation-state mechanism.**

**Schema v2** (`storage/schema_v2.sql`, applied by `storage/db.py::connect()` after `schema_v1.sql`, guarded by a new `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)` table so each migration runs exactly once per database):

```sql
ALTER TABLE users ADD COLUMN age INTEGER;
ALTER TABLE users ADD COLUMN location TEXT;
ALTER TABLE users ADD COLUMN language TEXT;
ALTER TABLE users ADD COLUMN nationality TEXT;
ALTER TABLE users ADD COLUMN pending_registration_field TEXT;
```

All five are nullable with no `DEFAULT`, so every pre-existing row reads back as `NULL` on all five — an existing user is, correctly, "not registered" until they run the flow. `identity.models.UserRecord` grows the matching five optional fields plus two derived properties: `profile_complete` (`True` iff `age`, `location`, `language`, and `nationality` are all set — `display_name` does not count, since it is populated before registration ever runs) and `is_mid_registration` (`True` iff `pending_registration_field is not None`). A shared `ProfileField = Literal["display_name", "age", "location", "language", "nationality"]` type alias lives in `identity.models` next to `UserRecord`, used by `UserRecord`'s own fields and by the two new repository functions below.

**`bot.config.RegistrationStep.profile_field` stays a bare `str`, not `ProfileField`.** `tests/test_bot_config.py::test_load_config_accepts_a_sixth_registration_step` is #55's own proof that the config loader accepts a step naming a field nothing downstream maps yet ("adding ... a step requires no Python change") — tightening the loader to identity's five known columns would break that extensibility test and the property it locks. The cross-check this ticket needs — "does this deployed `data/bot.yaml` only declare steps this build's registration handler can actually fulfill?" — belongs where the *consumer* reads the steps, not where the *shape* is validated: `bot/registration.py` checks each configured step's `profile_field` against `identity.models.ProfileField`'s known values when it first builds its step lookup, and treats a step naming anything else as the same class of loud, internal failure `commands._stored_id` already uses for an impossible state — a config bug, not a user-facing outcome, and orthogonal to #55's guarantee that the *loader itself* tolerates an unknown field.

**One column is the whole state machine.** `pending_registration_field`:
- `NULL` means "nothing in flight" — either never started, or the last run of the flow finished.
- Any other value names the `ProfileField` the user's *next* message should be validated against.

Starting or resuming is the same code path: `/register` sets `pending_registration_field` to the first configured step's field **only if it is currently `NULL`**; if it is already set, `/register` re-sends that step's prompt and changes nothing, which is what makes an abandoned flow resumable by *either* sending a plain answer or re-running `/register` — both read the same pointer. A plain-text message is dispatched to the registration continuation handler only when `pending_registration_field` is not `NULL` for its sender; otherwise it is inert, exactly the behavior #57 needs to layer its own routing onto without touching this mechanism. On a valid answer, the target column and the pointer's next value are written in one statement, so a crash between "the answer landed" and "the pointer advanced" cannot happen — there is no intermediate state to crash between. `/me`'s "has this user registered" check reads `profile_complete`, not the pointer, so a user restarting registration to correct an existing answer keeps seeing their still-fully-populated profile — including the fields not yet re-answered in the new pass — until the very last step of the re-run flips the pointer back to `NULL`.

**ADR numbering.** `0011-telegram-bot-interface.md`'s Context reserves 0012 (in prose only, no file exists on `main`) for a future Meetup/Eventbrite decision. This ADR takes **0013** to respect that reservation. It is filed with the awareness that an unmerged branch (`feat/scheduled-ingestion` and variants) has independently already used both 0012 and 0013 for an unrelated extraction decision; whichever of the two branches merges to `main` second will need to renumber, following the redirect-paragraph precedent ADR 0011 itself set. Tracked as a repo-hygiene issue (#80), not blocking here.

## Consequences

### Positive

- One mechanism, already-accepted precedent. No session table, no new aggregate, no join — `identity/`'s existing "one row per Telegram user" shape absorbs registration state the same way it already absorbs identity.
- Resumability is a property of the data, not of a running process or a timeout. A restart, a crash between messages, or an hours-long gap all look identical to the flow: the pointer is exactly where the last successful write left it.
- #57 and #58 depend on nothing new: both already resolve the sender's `UserRecord` before deciding what to do with a message, and `is_mid_registration` is right there on it.
- Closes ADR 0003's own named gap (no schema-version table) with the ticket that first needed one, rather than as a speculative preemptive change.
- The "does this step name a field we can fulfill" check lands where the consumer is, so #55's extensibility test (`test_load_config_accepts_a_sixth_registration_step`) keeps proving what it was written to prove, undisturbed by this ADR.

### Negative / accepted trade-offs

- **`identity.UserRecord` carries one field — `pending_registration_field` — that is bot-flow state, not an identity fact.** Accepted: the alternative (a second table for one nullable column) costs a join for no relational benefit, and the field is still meaningless outside the context of a `users` row.
- **`ALTER TABLE ADD COLUMN` has no `IF NOT EXISTS` in SQLite** (verified against the stdlib's bundled 3.45.1 — the clause is a syntax error, not a no-op), so idempotency has to come from the new `schema_migrations` table rather than from the statement itself. This is exactly the version-tracking mechanism ADR 0003 flagged as owed to the first v2 change; it did not exist before this ADR and now it does, for every schema change after this one too.
- **A user who restarts registration shows their old, not-yet-overwritten values through `/me` while the re-run is in progress.** Accepted as the more correct of two imperfect options: the alternative (hiding a complete-but-being-updated profile behind "register first" the moment `/register` is re-run) reports a false negative about data that is still entirely valid.

### Follow-ups

- **#57** reads `UserRecord.is_mid_registration` before deciding whether a plain-text message goes to the agent loop or is left for the registration continuation handler.
- **#58**'s per-user queue serializes registration-answer messages the same way it serializes everything else; nothing here requires the queue to know registration exists.
- **#80** — reconcile the ADR-numbering collision between this ADR, the reservation in ADR 0011, and the unmerged `feat/scheduled-ingestion` branch's own 0012/0013, whichever side merges to `main` second.
