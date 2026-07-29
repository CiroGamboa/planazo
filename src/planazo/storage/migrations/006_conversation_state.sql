-- Planazo domain store, schema v6 (issue #M37T2).
--
-- Adds the `conversation_state` table — the per-user scratchpad the
-- multi-turn `/find` conversation service reads and upserts on every
-- message. One row per user (`user_id PRIMARY KEY`) means the service
-- upserts in place; a second row for the same user is a schema
-- violation, not a legitimate follow-up.
--
-- `pending_clarification` is a JSON blob when populated
-- (`{"question": ..., "intent_snapshot": SearchIntent JSON}`) — the
-- shape `PendingClarification.model_dump_json()` emits — and NULL when
-- no clarification is in flight. `service.handle_user_message` clears
-- the column to NULL as soon as the user's next message is consumed
-- as the answer.
--
-- `last_recommendation_run_id` points at the most recent Recommender
-- loop that surfaced candidates for this user. Backs the two
-- follow-up patterns: "tell me about #N" (look up the Nth
-- `recommendations` row for that run) and "more results" (re-run the
-- same query, filter out event IDs already surfaced under that
-- run_id). NULL means no prior recommendations run — a fresh user, or
-- a user whose only prior interactions were clarification questions.
--
-- Foreign keys:
-- * `user_id` → `users(id)`. A row without its parent user makes no
--   sense — the row's whole purpose is per-user state. `PRAGMA
--   foreign_keys = ON` is set in `db.connect()`.
--
-- Note on `last_recommendation_run_id`: no FK to `agent_runs.run_id`.
-- Deleting an `agent_runs` row is not a supported operation
-- (observability writes are append-only), so a dangling reference is
-- not a real failure mode; a future retention sweep that changes that
-- can add the FK in its own migration.
--
-- Indexes:
-- * `idx_conversation_state_updated` backs future operator queries by
--   activity ("which users had a conversation in the last 24 hours").
--   Non-unique — many users can share a timestamp under concurrent
--   traffic.

CREATE TABLE conversation_state (
    user_id                     INTEGER PRIMARY KEY REFERENCES users(id),
    pending_clarification       TEXT,
    last_recommendation_run_id  TEXT,
    updated_at                  TEXT    NOT NULL
);

CREATE INDEX idx_conversation_state_updated ON conversation_state(updated_at);
