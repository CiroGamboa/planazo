-- Planazo domain store, schema v5 (issue #M37T1).
--
-- Adds the `recommendations` table — one row per candidate the Recommender
-- ranked and returned for a single loop. A completed `agent_runs` row with
-- `RecommenderResult.status in {"ok", "no_results"}` produces 0..N children
-- here (0 for `no_results`, one per candidate for `ok`), preserving the
-- ordering by `rank_position` starting at 0 for the top-ranked candidate.
--
-- The `reason` column carries the ranker's per-candidate rationale string
-- when the deterministic ranker is wired into `run_once`. Today
-- (M3.7 T1) the Recommender does not invoke `rank_events` — `run_once`
-- returns filtered but unranked candidates — so this ticket persists them
-- with `score = NULL` and `reason = NULL`; the score/reason columns exist
-- for the follow-up ticket that wires the ranker.
--
-- `reason` sits INSIDE the trust boundary (AGENTS.md Rule 2 → rationale
-- hook), matching the discipline established for `llm_decisions.rationale`:
-- full ranker reasoning is allowed subject to a 500-char cap +
-- `format_stored_text` sanitization enforced at the Pydantic boundary.
-- Redaction happens on the way OUT to any operator- or model-visible
-- surface, not on the way in.
--
-- Foreign keys:
-- * `run_id` → `agent_runs.run_id`. A `recommendations` row without its
--   parent run makes no sense — the run defines when the recommendation
--   was produced, the row records what was recommended. `PRAGMA
--   foreign_keys = ON` is set in `db.connect()`.
-- * `event_id` → `events.id ON DELETE SET NULL`. A future retention sweep
--   that deletes stale events must not cascade-delete the audit rows that
--   document that we once recommended them; setting the pointer to NULL
--   preserves the historical decision (rank_position, score, reason)
--   while releasing the FK.
--
-- Indexes:
-- * `idx_recommendations_run_rank` backs the "all candidates for one run,
--   in rank order" query shape — the join shape a `/find` history reader
--   or the "tell me about #N" pattern will use. Composite on
--   (run_id, rank_position) so the WHERE-then-ORDER-BY plan is index-only.

CREATE TABLE recommendations (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT    NOT NULL REFERENCES agent_runs(run_id),
    event_id      INTEGER          REFERENCES events(id) ON DELETE SET NULL,
    rank_position INTEGER NOT NULL,
    score         REAL,
    reason        TEXT,
    recorded_at   TEXT    NOT NULL
);

CREATE INDEX idx_recommendations_run_rank ON recommendations(run_id, rank_position);
