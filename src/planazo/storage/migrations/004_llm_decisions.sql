-- Planazo domain store, schema v4 (issue #90).
--
-- Adds the `llm_decisions` table — one row per terminal decision the LLM
-- produced during one Recommender or Extractor run. A single `agent_runs`
-- row typically has 0..N `llm_decisions` children:
--
-- * The Extractor emits one row per successful `save_event` call plus one
--   row per `report_extraction_status` call (`needs_clarification` / `error`).
--   A single run announcing three events on the same post produces three
--   `save_event` rows tied to the same `run_id`.
-- * The Recommender emits one `answered` row per successful loop.
-- * Both composition roots emit one `error` row on `stopped in {"truncated",
--   "max_steps"}` — the loop ran out of budget or was cut off mid-turn.
--
-- The rationale text sits INSIDE the trust boundary (AGENTS.md Rule 2 →
-- rationale hook): full LLM reasoning is allowed subject to the 500-char
-- cap + `format_stored_text` sanitization enforced at the Pydantic
-- boundary. Redaction happens on the way OUT (any operator-facing surface,
-- any future `/find` history projection) rather than on the way in — the
-- audit trail stays useful for post-hoc explainability of an extraction
-- decision.
--
-- Foreign keys:
-- * `run_id` → `agent_runs.run_id`. A `llm_decisions` row without its
--   parent run makes no sense — the run defines the LLM turn history, the
--   decision is what the LLM produced in that history. `PRAGMA
--   foreign_keys = ON` is set in `db.connect()`; the FK is enforced.
-- * `event_db_id` → `events.id ON DELETE SET NULL`. A future retention
--   sweep that deletes stale events must not cascade-delete the audit
--   rows that document how the LLM decided to save them; setting the
--   pointer to NULL preserves the rationale while releasing the FK.
--
-- Consistency between `decision_kind`, `event_db_id`, and `error_type`
-- is enforced at the Pydantic model boundary (`LLMDecision`'s
-- `model_validator`), not by a DB CHECK: the four Literal branches and
-- their required-field shapes are readable in one place at the Python
-- boundary, and a CHECK expression that mirrors them would be denser
-- than the model without adding a second surface a caller could bypass.
-- The `decision_kind` CHECK below is the defense-in-depth lock against a
-- raw-SQL diagnostic tool bypassing Pydantic entirely.
--
-- Indexes:
-- * `idx_llm_decisions_run` backs the "all decisions for one run" join —
--   the shape any post-hoc trace inspector will query on. Non-unique
--   because a single run may have many decisions.
-- * `idx_llm_decisions_kind` backs the "how often does the LLM produce
--   `multiple_events_in_post`" corpus-analysis shape M4's ranker will
--   read against. Non-unique — many rows per kind.

CREATE TABLE llm_decisions (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT    NOT NULL REFERENCES agent_runs(run_id),
    decision_kind TEXT   NOT NULL CHECK (decision_kind IN ('save_event', 'needs_clarification', 'error', 'answered')),
    event_db_id  INTEGER          REFERENCES events(id) ON DELETE SET NULL,
    error_type   TEXT,
    rationale    TEXT    NOT NULL,
    recorded_at  TEXT    NOT NULL
);

CREATE INDEX idx_llm_decisions_run ON llm_decisions(run_id);
CREATE INDEX idx_llm_decisions_kind ON llm_decisions(decision_kind);
