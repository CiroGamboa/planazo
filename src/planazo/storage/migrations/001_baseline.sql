-- Planazo domain store, schema v1 (docs/adr/0003-sqlite-domain-store.md).
--
-- `db.connect()` runs this whole script through `executescript` on every open,
-- so every statement is `IF NOT EXISTS`: applying it to a database that already
-- has these tables is a no-op.
--
-- Timestamps are ISO-8601 TEXT. `events.extra` is a JSON-encoded object in a
-- TEXT column — SQLite's JSON1 is a set of functions over TEXT, not a distinct
-- column type.

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY,
    source              TEXT    NOT NULL,
    source_url          TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    start_utc           TEXT    NOT NULL,
    end_utc             TEXT    NOT NULL,
    category            TEXT    NOT NULL,
    city                TEXT    NOT NULL,
    price_cents         INTEGER NOT NULL DEFAULT 0,
    geo_lat             REAL,
    geo_lng             REAL,
    confidence          REAL    NOT NULL,
    extra               TEXT    NOT NULL DEFAULT '{}',
    ingested_at         TEXT    NOT NULL,
    event_index_in_post INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_url, event_index_in_post)
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    telegram_user_id TEXT    NOT NULL UNIQUE,
    display_name     TEXT    NOT NULL,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    artifact_kind TEXT    NOT NULL,
    artifact_id   INTEGER NOT NULL,
    decision      TEXT    NOT NULL,
    decided_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_runs_index (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT    NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    url        TEXT    NOT NULL,
    started_at TEXT    NOT NULL
);

-- `scan_state` holds one row per source URL the scheduler has scanned. The
-- primary key is `source_url`: both post entries (from `sources.instagram.posts:`)
-- and account entries (from `sources.instagram.accounts:`) share the table
-- because their bookkeeping shape is identical. Timestamps are ISO-8601 TEXT
-- to match every other table. This is a `CREATE TABLE IF NOT EXISTS` — an
-- existing dev database without this table picks it up on the next
-- `db.connect()`; a stale dev database with an earlier column shape needs
-- deletion before the next open (the events schema followed the same pattern
-- when #64 landed).
CREATE TABLE IF NOT EXISTS scan_state (
    source_url           TEXT    PRIMARY KEY,
    last_scanned_at      TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
