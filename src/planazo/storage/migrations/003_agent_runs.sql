-- Planazo domain store, schema v3 (issue #89).
--
-- Adds the `agent_runs` table — one row per completed Recommender or Extractor
-- run — plus the composite index the `/find` history reader (M6 #23) will
-- issue against. The runs table sits alongside the JSONL sidecars under
-- `data/runs/` and `var/extraction_runs.jsonl`: SQLite carries the fields the
-- Recommender + operator need to query relationally (per-user history, kind
-- filter, timespan), while the JSONL sidecars keep the full trace grain.
--
-- `agent_kind` is `CHECK`-constrained to `('recommender', 'extractor')` —
-- the Pydantic `AgentRunRecord.agent_kind` Literal is the ergonomic front
-- door and the CHECK is the defense-in-depth boundary lock for callers who
-- bypass the aggregate (raw SQL in a diagnostic tool, a mid-migration
-- backfill, etc). `user_id` is nullable because the Extractor's synthetic
-- system user (`SYSTEM_USER_TELEGRAM_ID`) is not seeded on every dev DB and
-- a null attribution is a legitimate branch (an operator-triggered run
-- outside any Telegram session). `final_answer` is nullable because a
-- `stopped='max_steps'` termination leaves `LoopResult.answer` as `None`.
--
-- `idx_agent_runs_user_started` backs the "history for one user, most
-- recent first" query shape. Non-unique — a single user typically has many
-- runs, and two runs sharing a millisecond timestamp are possible under
-- concurrent scheduler + interactive sessions.

CREATE TABLE agent_runs (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT    NOT NULL UNIQUE,
    agent_kind   TEXT    NOT NULL CHECK (agent_kind IN ('recommender', 'extractor')),
    user_id      INTEGER          REFERENCES users(id),
    user_query   TEXT    NOT NULL,
    final_answer TEXT,
    stopped      TEXT    NOT NULL,
    steps_count  INTEGER NOT NULL,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    NOT NULL
);

CREATE INDEX idx_agent_runs_user_started ON agent_runs(user_id, started_at);
