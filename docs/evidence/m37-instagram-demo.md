# M3.7 T3 — Instagram narrative demo evidence

- **Date:** 2026-07-29
- **Ticket:** M3.7 T3 — `feat(sources,docs): narrative demo logs + instagram README`
- **Branch commit at capture:** `963be6d` (branch cut point `feat/m37-t3-instagram-demo` off `origin/main`)
- **Target URL:** `https://www.instagram.com/p/DbSiUpoDNiZ/`

## Command run

```bash
uv run planazo-scheduler --once https://www.instagram.com/p/DbSiUpoDNiZ/ --verbose
```

Environment: operator's `.env` had `PLANAZO_IG_HIKER_API_KEY_1..3` and `OPENCODE_API_KEY` set. `INSTAGRAM_SESSION_ID` unset (public post, anonymous fetch worked).

## Actual stdout (verbatim)

```
[01:07:03] Fetching post DbSiUpoDNiZ from Instagram...
[01:07:11] Fetched post - 3 media asset(s)
[01:07:15] Reported needs_clarification: multiple_events_in_post
[01:07:19] Loop terminated: stopped=answered, steps=3
{"run_id":"ba2601e5-af48-489b-b2c1-12f48d052785","source_url":"https://www.instagram.com/p/DbSiUpoDNiZ/","source_kind":"post","backend":null,"gate_reason":"first_run","posts_discovered":0,"posts_extracted_ok":0,"posts_extracted_error":1,"posts_skipped_idempotent":0,"errors":["not_found: Weekly agenda carousel lists several distinct events across dates/venues; extraction policy requires clarification for c"],"started_at":"2026-07-29T01:07:03.564782Z","ended_at":"2026-07-29T01:07:19.611164Z"}
```

Wall-clock total: ~16 seconds (fetch + carousel LLM turn + report). Cost: ~$0.03-0.04 STRONG-tier LLM (single turn on a 3-slide carousel).

## Observations

- **Narrative shape validated end-to-end.** The four `[HH:MM:SS]` lines describe the full extraction lifecycle: fetch start → fetch complete with 3 assets → LLM signalled clarification (multi-event carousel) → loop terminated.
- **Rule 2 discipline held.** Every narrative line carries only shortcodes, integer counts, and Literal-valued fields (`multiple_events_in_post`, `needs_clarification`, `answered`, `steps=3`). No caption bytes, no event titles, no LLM rationale.
- **The `SchedulerRunRecord` JSON line is unchanged.** It fires after the narrative regardless of `--verbose` — cron shape preserved.
- **The `errors` field in the JSON record still carries a paraphrased LLM `notes` string** (`"Weekly agenda carousel lists several distinct events..."`) — that's the pre-existing `SchedulerRunRecord.errors` field, DB-inside per ADR 0015. The narrative stream deliberately does not echo it (ADR 0017 §3).

## Operator tweaks

- **Backend.** For a business-account URL, edit `data/sources.yaml` and set `backend: "hikerapi"` on the `AccountConfig`. HikerAPI cost is ~$0.03/request.
- **ffmpeg presence.** Required on the host `PATH` for reel URL demos; `brew install ffmpeg` on macOS. Not needed for static posts / carousels.
- **Target account.** Any public Instagram post URL works. Swap `p/` for `reel/` for a reel demo. For a fresh URL, expect ~$0.02-0.08 in LLM cost per successful extraction; already-persisted URLs short-circuit at the idempotency pre-check for free.
- **Silence the narrative for cron.** Omit `--verbose`; `--once` then prints exactly one JSON line (the `SchedulerRunRecord`) to stdout — byte-compatible with the M3.5 shape.

## Cross-references

- [ADR 0017 — Instagram demo narrative logs](../adr/0017-instagram-demo-narrative-logs.md) — the four decisions (opt-in / stdout-only / structural-only / observer-seam) this evidence file demonstrates.
- [`src/planazo/sources/instagram/README.md`](../../src/planazo/sources/instagram/README.md) — the one-page module guide the demo commands paste from.
- [ADR 0015 — Storage migrations and observability](../adr/0015-storage-migrations-and-observability.md) — the DB-inside audit surface the narrative log complements.
