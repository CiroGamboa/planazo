-- Guided registration profile columns (docs/adr/0018-registration-conversation-state.md).
--
-- Four nullable profile fields plus one pointer to whichever field the user's
-- next message should answer. All five are nullable with no DEFAULT, so every
-- pre-existing row reads back as NULL on all five until that user runs the
-- flow.
--
-- `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` clause, so these
-- statements are not independently idempotent. The migration runner in
-- `storage/db.py` wraps this file in a single `BEGIN` / `COMMIT` together with
-- its `PRAGMA user_version` bump, so a failure partway through rolls the whole
-- batch back and leaves `user_version` at 6 — "columns added, version not
-- recorded" is unreachable.

ALTER TABLE users ADD COLUMN age INTEGER;
ALTER TABLE users ADD COLUMN location TEXT;
ALTER TABLE users ADD COLUMN language TEXT;
ALTER TABLE users ADD COLUMN nationality TEXT;
ALTER TABLE users ADD COLUMN pending_registration_field TEXT;
