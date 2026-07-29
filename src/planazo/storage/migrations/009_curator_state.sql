-- Catalog-curator singleton state (docs/adr/0020-catalog-curator-agent.md).
--
-- The curator has one row of persistent bookkeeping: when did it last run,
-- when did it last succeed, how many ticks have failed in a row, and a
-- running tally of the mutations it has performed. `CHECK (id = 1)` locks
-- the row to a singleton — the curator is a system-wide steward, not
-- per-user or per-URL.
--
-- The row is seeded here via `INSERT OR IGNORE` so a fresh database always
-- reads back a defaults-only row (matches the pattern `scan_state` uses
-- for its per-URL bookkeeping). Every subsequent tick upserts the whole
-- row via `curator/repository.py::upsert_state`.
--
-- Timestamps are ISO-8601 TEXT to match every other table; counters are
-- INTEGERs starting at zero.

CREATE TABLE curator_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_at            TEXT,
    last_success_at        TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    total_archived         INTEGER NOT NULL DEFAULT 0,
    total_merged           INTEGER NOT NULL DEFAULT 0,
    total_categories_fixed INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO curator_state (id) VALUES (1);
