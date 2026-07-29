-- Extend `agent_runs.agent_kind` CHECK to include 'curator'
-- (docs/adr/0020-catalog-curator-agent.md).
--
-- SQLite does not support `ALTER TABLE ... ALTER CONSTRAINT`, so this
-- migration follows the standard "rebuild the table" pattern: create the
-- new-shape table, copy every row over verbatim, drop the old table,
-- rename the new one, and recreate the composite index. The migration
-- runner in `storage/db.py` wraps this in one `BEGIN`/`COMMIT` batch so
-- a mid-rebuild failure rolls back to the pre-migration shape — a
-- half-rebuilt schema is unreachable.
--
-- The rebuild is also the migration for `decision_kind` on
-- `llm_decisions` — extending its CHECK to include the curator's three
-- new terminal decisions (`'archive'`, `'merge'`, `'update_category'`)
-- alongside the existing four. Same rebuild-and-copy pattern; same
-- transactional guarantee.

-- ---- agent_runs -----------------------------------------------------------

CREATE TABLE agent_runs_new (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT    NOT NULL UNIQUE,
    agent_kind   TEXT    NOT NULL CHECK (agent_kind IN ('recommender', 'extractor', 'curator')),
    user_id      INTEGER          REFERENCES users(id),
    user_query   TEXT    NOT NULL,
    final_answer TEXT,
    stopped      TEXT    NOT NULL,
    steps_count  INTEGER NOT NULL,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    NOT NULL
);

INSERT INTO agent_runs_new (
    id, run_id, agent_kind, user_id, user_query, final_answer, stopped,
    steps_count, started_at, ended_at
)
SELECT
    id, run_id, agent_kind, user_id, user_query, final_answer, stopped,
    steps_count, started_at, ended_at
FROM agent_runs;

DROP TABLE agent_runs;
ALTER TABLE agent_runs_new RENAME TO agent_runs;

CREATE INDEX idx_agent_runs_user_started ON agent_runs(user_id, started_at);

-- ---- llm_decisions --------------------------------------------------------

CREATE TABLE llm_decisions_new (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT   NOT NULL REFERENCES agent_runs(run_id),
    decision_kind TEXT   NOT NULL CHECK (decision_kind IN (
        'save_event', 'needs_clarification', 'error', 'answered',
        'archive', 'merge', 'update_category'
    )),
    event_db_id   INTEGER          REFERENCES events(id) ON DELETE SET NULL,
    error_type    TEXT,
    rationale     TEXT   NOT NULL,
    recorded_at   TEXT   NOT NULL
);

INSERT INTO llm_decisions_new (
    id, run_id, decision_kind, event_db_id, error_type, rationale, recorded_at
)
SELECT
    id, run_id, decision_kind, event_db_id, error_type, rationale, recorded_at
FROM llm_decisions;

DROP TABLE llm_decisions;
ALTER TABLE llm_decisions_new RENAME TO llm_decisions;

CREATE INDEX idx_llm_decisions_run ON llm_decisions(run_id);
CREATE INDEX idx_llm_decisions_kind ON llm_decisions(decision_kind);
