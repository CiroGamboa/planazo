-- Planazo domain store, schema v2 (docs/adr/0013-registration-conversation-state.md).
--
-- Five new nullable `users` columns backing the guided registration flow:
-- four profile fields plus one pointer to whichever field the user's next
-- message should answer. All five are nullable with no DEFAULT, so every
-- pre-existing row reads back as NULL on all five until that user runs the
-- flow.
--
-- Unlike schema_v1.sql, SQLite's `ALTER TABLE ... ADD COLUMN` has no
-- `IF NOT EXISTS` clause, so these statements are not independently
-- idempotent and `db.connect()` cannot run this file through
-- `executescript()` the way it does schema_v1.sql. `storage/db.py` applies
-- them itself, exactly once per database, inside one transaction guarded by
-- the `schema_migrations` table.

ALTER TABLE users ADD COLUMN age INTEGER;
ALTER TABLE users ADD COLUMN location TEXT;
ALTER TABLE users ADD COLUMN language TEXT;
ALTER TABLE users ADD COLUMN nationality TEXT;
ALTER TABLE users ADD COLUMN pending_registration_field TEXT;
