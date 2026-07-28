-- Planazo domain store, schema v2 (issue #88).
--
-- Extends `events` from the M3 baseline shape into the full domain model the
-- Recommender (M4) filters + scores against: the source-account handle that
-- posted the flyer, the named venue + address, the promoter/organizer, a JSON
-- array of tags/genres, the LLM's caption paraphrase, ticketing/image URLs,
-- an ISO-639 language tag, and a recurring-series marker.
--
-- SQLite's `ALTER TABLE ADD COLUMN` handles this one column per statement.
-- Every added column is either nullable or carries an explicit `NOT NULL
-- DEFAULT`, so existing rows migrate without a backfill. `db.connect()` wraps
-- the whole script in one `BEGIN; ... COMMIT;` transaction together with
-- `PRAGMA user_version = 2` — a mid-migration failure rolls the whole thing
-- back and leaves the file at `user_version = 1`.
--
-- Composite indexes back the two hot filter shapes the Recommender issues:
-- "events in <city> starting after <t>" and "<category> events starting after
-- <t>". Both are non-unique — same-city or same-category rows coexist.

ALTER TABLE events ADD COLUMN source_account TEXT;
ALTER TABLE events ADD COLUMN venue_name     TEXT;
ALTER TABLE events ADD COLUMN venue_address  TEXT;
ALTER TABLE events ADD COLUMN organizer      TEXT;
ALTER TABLE events ADD COLUMN tags           TEXT    NOT NULL DEFAULT '[]';
ALTER TABLE events ADD COLUMN description    TEXT;
ALTER TABLE events ADD COLUMN ticket_url     TEXT;
ALTER TABLE events ADD COLUMN image_url      TEXT;
ALTER TABLE events ADD COLUMN language       TEXT;
ALTER TABLE events ADD COLUMN recurring      INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_events_city_start     ON events(city, start_utc);
CREATE INDEX IF NOT EXISTS idx_events_category_start ON events(category, start_utc);
