# Catalog curator agent

This module implements Planazo's **admin-scoped LLM agent** that periodically cleans the `events` catalog. It has capabilities the Recommender and Extractor deliberately don't: **write access to arbitrary event rows** via soft-delete + category update. Governed by [ADR 0020](../../../docs/adr/0020-catalog-curator-agent.md).

## Charter

Three cleanup jobs, all on the shared `events` table:

1. **Archive stale events** — anything past its `end_utc` shouldn't surface to `/find` users. The curator soft-deletes them (`events.archived_at` gets a timestamp; the primary key is preserved so the audit trail stays intact).
2. **Merge obvious duplicates** — the same event announced by two accounts (venue + promoter) sharing normalized(title) + start-date + venue. The curator picks the highest-confidence row as canonical and archives the rest.
3. **Fix mis-classified categories** — low-confidence extractions where the title clearly maps to a different `EventCategory` Literal.

Every write is **soft** and **reversible**: `UPDATE events SET archived_at = NULL WHERE id = <id>` restores any archived row.

## Quick commands

```bash
# Dry-run (safe demo — no DB writes; LLM sees the same tools but writes return
# {"status": "dry_run", ...}). Costs one STRONG-tier LLM turn (~$0.02-0.05).
uv run planazo-curator --tick --dry-run --verbose

# Real tick (soft-deletes + category updates go through).
uv run planazo-curator --tick

# Alias via python -m
uv run python -m planazo.curator --tick --dry-run
```

## Cron

Suggested crontab entry for daily off-peak curator ticks:

```cron
0 3 * * * cd /path/to/planazo && uv run planazo-curator --tick >> var/curator.log 2>&1
```

3 AM UTC is off-peak for Barcelona operators. `--verbose` off (cron shape stays one-line-per-tick). Redirect stdout+stderr to `var/curator.log`; the structured audit trail lives in `var/curator_runs.jsonl`.

## Tool inventory

| Tool | Kind | Behavior |
|------|------|----------|
| `list_stale_events(limit=50)` | read | Events with `end_utc < now` AND `archived_at IS NULL`. |
| `list_duplicate_candidates(limit=50)` | read | Groups by `(lower(trim(title)), start_utc::date, coalesce(venue_name, ""))` with count > 1. |
| `list_low_confidence_events(threshold=0.4, limit=50)` | read | `confidence < threshold` on live rows. |
| `archive_event(event_id, reason)` | write | Soft-delete one row. |
| `merge_events(keep_event_id, archive_event_ids, reason)` | write | Archive N rows, keep one canonical. Refuses partial merges. |
| `update_event_category(event_id, new_category, reason)` | write | Correct an `EventCategory` Literal on a live row. |

Every write requires a `reason` (≤ 500 chars). Rationale is DB-inside per Rule 2 — sanitized via `format_stored_text`, stored in `llm_decisions.rationale`. See ADR 0015 for the trust-boundary discipline.

## What you should see

Sample `--verbose` output shape (real ids and rationales redacted):

```
[01] list_stale_events(...) -> 4 row(s)
[02] archive_event(event_id=17) -> ok
[03] archive_event(event_id=19) -> ok
[04] list_duplicate_candidates(...) -> 1 row(s)
[05] merge_events(keep=32, archive=1 id(s)) -> ok
[06] list_low_confidence_events(...) -> 2 row(s)
[07] update_event_category(event_id=41, new_category='cultural') -> ok
[--] loop terminal: stopped=answered, steps=7
tick: run_id=a4b1c2d3 stopped=answered steps=7 archived=3 merged=1 updated=1 errors=0 dry_run=False
```

The single last line is the tick summary — always emitted, `--verbose` or not. The `[NN]` prefixed lines only fire under `--verbose`.

**Rule 2 discipline (ADR 0017-style).** The narrative log carries only ids, counts, Literal-valued fields, and structural markers. It NEVER interpolates `Event.title`, `Event.description`, `Event.venue_name`, or the LLM's `reason` argument (which contains free-form judgment about the caption content). Full sanitized rationale lives in `llm_decisions.rationale` inside the DB trust boundary; the demo transcript stays structural.

## Cost expectations

Per tick:
- **LLM turn:** STRONG tier (`gpt-5.4-strong`), ~$0.02-0.05 depending on how many events the read tools return.
- **Bounded:** `max_steps=12` — the loop is guaranteed to terminate. A tick that runs out of steps gets `stopped=max_steps` and comes back tomorrow.
- **Daily cadence at 3 AM UTC → ~$0.90/month.**

## What lands where

- **`agent_runs`** — one row per tick with `agent_kind='curator'`, `user_id=NULL` (system-owned).
- **`llm_decisions`** — one row per successful write-tool call. `merge_events` produces N rows (one per archived id). `decision_kind` is one of `archive`, `merge`, `update_category`. Rationale = the LLM's `reason` argument.
- **`curator_state`** — the singleton bookkeeping row (`id=1`). Bumps `last_run_at`; bumps `last_success_at` and resets `consecutive_failures` only on `stopped=answered`.
- **`events.archived_at`** — the soft-delete column T1 added. Non-NULL means "curator retired this row" (or an operator did — the primitive is source-agnostic).
- **`var/curator_runs.jsonl`** — one `CuratorRunRecord` line per tick with counters + errors + `dry_run` flag.

## Troubleshooting

- **`stopped=truncated` or `max_steps`.** The LLM ran out of turns. Check `consecutive_failures` in `curator_state`; three in a row is the operational-alert threshold.
- **`llm_decisions.rationale` looks generic.** The LLM's `reason` argument was empty or generic. The tool tier accepts anything non-empty ≤ 500 chars; if you want better rationales, refine the system prompt in `curator/agent.py`.
- **A soft-deleted event needs to come back.** `UPDATE events SET archived_at = NULL WHERE id = <id>` — the primitive `restore_event` in `catalog/repository.py` does exactly that.
- **Dry-run doesn't produce `llm_decisions` rows.** By design — dry-run writes return `{"status": "dry_run", ...}` which the observer skips. The tick's `agent_runs` row is still written; use it to see what the LLM decided.

## Related docs

- [ADR 0020 — Catalog curator agent](../../../docs/adr/0020-catalog-curator-agent.md).
- [ADR 0015 — Storage migrations + observability](../../../docs/adr/0015-storage-migrations-and-observability.md) — the observability discipline the curator reuses.
- [ADR 0017 — Instagram demo narrative logs](../../../docs/adr/0017-instagram-demo-narrative-logs.md) — the Rule-2 pattern the `--verbose` stream follows.
- [`../../../docs/HOW-TO-TEST-BOT-E2E.md`](../../../docs/HOW-TO-TEST-BOT-E2E.md) — includes a section on running the curator alongside the bot.
