-- Soft-delete column for the catalog curator (docs/adr/0020-catalog-curator-agent.md).
--
-- One nullable timestamp column on `events`. `archived_at IS NULL` means the
-- event is live and reachable by `query_events` / `search_events`; a non-NULL
-- ISO-8601 timestamp means the curator (or an operator) soft-deleted it.
-- Every existing row reads back as NULL — pre-existing events remain live.
--
-- `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` clause. The migration
-- runner in `storage/db.py` wraps this file in one `BEGIN` / `COMMIT` batch
-- together with its `PRAGMA user_version` bump, so a partial failure rolls
-- the whole batch back — "column added, version not recorded" is unreachable.

ALTER TABLE events ADD COLUMN archived_at TEXT;
