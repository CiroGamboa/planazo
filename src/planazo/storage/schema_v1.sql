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
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    source_url  TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    start_utc   TEXT    NOT NULL,
    end_utc     TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    city        TEXT    NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0,
    geo_lat     REAL,
    geo_lng     REAL,
    confidence  REAL    NOT NULL,
    extra       TEXT    NOT NULL DEFAULT '{}',
    ingested_at TEXT    NOT NULL
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
