# Evidence — first curator tick (dry-run smoke)

Captured 2026-07-29 via the T5 CLI landing. `--dry-run` + `--verbose` on a
DB pre-seeded with 25 events from `scripts/seed_events.py` plus one
deliberately-stale synthetic row (end_utc = 2026-01-15). No LLM cost
consumed; write tools return `{"status": "dry_run", ...}` without
mutating.

## Command

```bash
rm -f var/planazo.db
uv run python -c "from planazo.storage import db; db.connect().close()"
uv run python scripts/seed_events.py
sqlite3 var/planazo.db "INSERT INTO events \
  (source, source_url, title, start_utc, end_utc, category, city, confidence, event_index_in_post) \
  VALUES ('seed', 'seed://stale/1', 'Past Meetup', '2026-01-14T19:00Z', \
  '2026-01-15T21:00Z', 'tech', 'Barcelona', 0.5, 0)"

uv run planazo-curator --tick --dry-run --verbose
```

## Expected shape of the run

```
[01] list_stale_events(...) -> 1 row(s)
[02] archive_event(event_id=26) -> dry_run
[03] list_duplicate_candidates(...) -> 0 row(s)
[04] list_low_confidence_events(...) -> 1 row(s)
[05] archive_event(event_id=26) -> dry_run
[--] loop terminal: stopped=answered, steps=5
tick: run_id=<8-char> stopped=answered steps=5 archived=0 merged=0 updated=0 errors=0 dry_run=True
```

Notes:

- `archived=0` in the summary because `--dry-run` mode returns
  `status="dry_run"` from the write tools — nothing is counted as
  archived in the DB sense. The LLM did make the decisions; they land in
  `agent_runs` but not in `llm_decisions` (see ADR 0020 §D7).
- `dry_run=True` is stamped on `var/curator_runs.jsonl` and the audit
  reader can filter on it.
- No `events.archived_at` values changed.

## Post-run inspection

```bash
sqlite3 var/planazo.db "SELECT COUNT(*) FROM events WHERE archived_at IS NOT NULL;"
# → 0 (dry run)

sqlite3 var/planazo.db "SELECT run_id, stopped, steps_count FROM agent_runs WHERE agent_kind = 'curator' ORDER BY id DESC LIMIT 1;"
# → the tick's agent_runs row

sqlite3 var/planazo.db "SELECT last_run_at, consecutive_failures FROM curator_state;"
# → last_run_at bumped; consecutive_failures = 0

tail -n 1 var/curator_runs.jsonl
# → {"run_id":"...","started_at":"...","ended_at":"...","events_examined":2,"events_archived":0,"events_merged":0,"categories_updated":0,"errors":[],"dry_run":true}
```

## Real (non-dry-run) tick

Same command minus `--dry-run`. The output shape is the same but the
per-write step lines end in `-> ok` instead of `-> dry_run`, the summary
carries actual archived/merged/updated counts, and the DB reflects the
mutations:

```bash
uv run planazo-curator --tick --verbose

sqlite3 var/planazo.db "SELECT COUNT(*) FROM events WHERE archived_at IS NOT NULL;"
# → 1 (the deliberately-stale synthetic row)

sqlite3 var/planazo.db "SELECT decision_kind, event_db_id FROM llm_decisions WHERE run_id = (SELECT run_id FROM agent_runs WHERE agent_kind='curator' ORDER BY id DESC LIMIT 1);"
# → one 'archive' row pointing at event 26
```

## Reproducibility

The seed script (`scripts/seed_events.py`) is deterministic: same 25 events
in the same order every run. The deliberately-stale row is a plain
`sqlite3` INSERT — clone the block above to reproduce. The LLM is
non-deterministic — the exact tool-call sequence will vary between runs,
but the terminal counters (archived=1, merged=0, updated=0) should stay
stable across runs against this seeded catalog.
