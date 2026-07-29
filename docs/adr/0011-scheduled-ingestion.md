# ADR 0011: Scheduled Instagram ingestion + per-shape extraction discipline

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Landed by:** M3.5

## Context

M3 shipped `extract_once(url, delegator_user_id) -> ExtractionResult` — the Extractor's front door for a single Instagram post, driven by the [ADR 0005](0005-multi-agent-shape.md) delegation brief, calling `fetch_instagram_post`, `save_event`, and `report_extraction_status` under a Recommender-side `dispatch_extraction` tool. What M3 did **not** ship: a discovery layer that walks target-account timelines, a scheduling clock that drives repeated ticks against the account list, an idempotency contract that keeps STRONG-tier LLM budget off already-persisted URLs, and multi-shape extraction quality good enough for curator carousels and text-on-video reels — the exact content curator accounts (`@bcn.agenda`, `@sala_apolo`, `@razzmatazz`, `@curated.agenda`) predominantly post.

M3.5 adds the missing pieces:

- **Discovery** — a `list_recent_posts(account_url) -> list[PostRef]` primitive on `InstagramSource`.

**§Context partially superseded by ADR 0014 (#67) — discovery is now split across two backends (anonymous `curl_cffi` + multi-key HikerAPI pool) routed at the scheduler composition root via `AccountConfig.backend`; the discovery seam lives in the `scheduler/` bounded context, not on `InstagramSource`.**
- **Clock** — host-cron `planazo-scheduler --tick` that reads `data/sources.yaml`, respects per-account cadence via `next_run_after`, and drives one pass through the account list per invocation.
- **Idempotency** — a pre-`extract_once` check that skips URLs already persisted to `events`.
- **Multi-shape extraction quality** — multi-event carousels (0..N events per post, [ADR 0012](0012-multi-event-extraction.md) / #64), multi-slide carousel LLM turn (up to 3 images for `GraphSidecar`, #65), and reel deep-extraction via extractor-side ffmpeg frame sampling ([ADR 0013](0013-extractor-side-frame-extraction.md) / #66).

Three architectural forks that earlier ADRs left open are settled here:

- **ADR 0005 §Decision 7** — "one image per call" — was empirically inadequate for `GraphSidecar` carousels where flyer text lands on slides 2+. #65 lifts the ceiling to `K=3` images for carousels only; this ADR records the semantics.
- **ADR 0005 §Decision 10** — "one post → at most one `Event`" — burned LLM budget on curator carousels announcing multiple distinct events. [ADR 0012](0012-multi-event-extraction.md) supersedes it with `ExtractionResult.events: list[Event]` and the composite `UNIQUE(source_url, event_index_in_post)` natural key; this ADR records the M3.5 motivation.
- **ADR 0006 §Decision 4** — "adapter never downloads binaries" — held for images but had to bend for reels, where the LLM needs frame content not just a URL. [ADR 0013](0013-extractor-side-frame-extraction.md) partially supersedes it: the ADAPTER still emits URL-only `MediaAsset` entries; the EXTRACTOR downloads the reel `video_url` inside `_multimodal_hook` and materializes JPEG frames via ffmpeg.

The nine decisions below are what M3.5 locks in — the scheduling shape, the idempotency source-of-truth, the state store, the system-user identity, and the four extraction-discipline decisions the scheduler depends on. Every decision names a rejected alternative with a reason ([`AGENTS.md`](../../AGENTS.md) rule 6).

## Decision

Planazo adopts a host-cron scheduled Instagram ingestion pipeline, driven by `planazo-scheduler --tick` reading `data/sources.yaml` and a new `scan_state` table, that per-account (a) discovers recent posts via `InstagramSource.list_recent_posts`, (b) pre-checks each URL against `events_exist_for_source_url` and skips already-persisted URLs before spending LLM budget, and (c) dispatches surviving URLs into `extract_once` under a seeded system user identity. Per-shape extraction quality is locked as: 0..N events per post (superseding ADR 0005 §D10 via ADR 0012); up to 3 images per LLM turn for `GraphSidecar` carousels (partially superseding ADR 0005 §D7); and extractor-side ffmpeg frame extraction (`MAX_REEL_FRAMES=3`) for reels (partially superseding ADR 0006 §D4 via ADR 0013). Scheduler ticks emit one `RunRecord`-shaped JSONL line per account into `var/scheduler_runs.jsonl`; failure handling is bounded by `scan_state.consecutive_failures >= 3` skipping the account for one tick.

The nine load-bearing decisions:

1. **Scheduling mode — host cron `planazo-scheduler --tick`.** Cron on the extractor host invokes the CLI once per tick; the process walks the account list, does its work, and exits. No long-running scheduler process.
   - **Rejected alternative — APScheduler in a long-running Docker service.** Cron matches M2's one-shot discipline (`docker compose up sources-instagram` is one-shot); deployment infrastructure (container orchestration, restart policy, health checks) is deferred. If the account list grows past what a single-machine cron can keep up with, ADR 0011-a will supersede this with a Docker-service mode.

2. **Idempotency source-of-truth — `events_exist_for_source_url` pre-check before every `extract_once` call.** For each `PostRef` returned by discovery, the scheduler calls `events_exist_for_source_url(conn, url)`; a non-empty return skips `extract_once` entirely.
   - **Rejected alternative — rely on `save_event`'s `duplicate_event` typed branch alone.** STRONG-tier LLM calls are ~$0.02 each; letting the model run the full extraction only to be caught by the `UNIQUE(source_url, event_index_in_post)` constraint at `save_event` time burns budget on every already-persisted URL. Pre-checking is O(1) SQL against the existing UNIQUE-derived index.

3. **State — dedicated `scan_state` table.** Columns: `account_url TEXT PRIMARY KEY, last_scanned_at TIMESTAMP, last_success_at TIMESTAMP, consecutive_failures INTEGER NOT NULL DEFAULT 0`. Read + upserted by the scheduler on every account visit.

   **§Decision 3 partially superseded by ADR 0014 (#67) — the primary-key column is `source_url` (not `account_url`); posts and accounts share the state table because their bookkeeping shape is identical.**
   - **Rejected alternative — derive scheduler state from `extraction_runs_index`.** `extraction_runs_index` is per-run (one row per `extract_once` invocation), not per-account; deriving last-scan / last-success timestamps requires grouping across all rows for an account, which is slow at scale and forces the scheduler to reason about per-run failure semantics it does not own.

4. **System user identity — seeded `users` row with `telegram_user_id="system"`, `display_name="Scheduled Scanner"`.** Bootstrapped idempotently on first `--tick` via `identity/repository.py::get_or_create_user`. Passed as `delegator_user_id` to every `extract_once` call the scheduler makes.
   - **Rejected alternative — pass `NULL delegator_user_id` for scheduler-driven runs.** [ADR 0004](0004-three-store-memory-model.md) pins every extraction to a user identity — the memory tools' `build_memory_tools(user_id)` closure discipline depends on it, and `extraction_runs_index.user_id` is a FK. A synthetic system user preserves the invariant with zero code change downstream.

5. **Multi-event support supersedes ADR 0005 §Decision 10.** One post → 0..N events via `ExtractionResult.events: list[Event]` + composite `UNIQUE(source_url, event_index_in_post)`. **Already landed via [ADR 0012](0012-multi-event-extraction.md) (#64).**
   - **Rejected alternative — keep the 0..1 cardinality and file multi-event support as a post-M3.5 follow-up.** Event-curator accounts routinely post multi-event carousels; under the 0..1 shape every such carousel returns `report_extraction_status(status="needs_clarification", error_type="multiple_events_in_post", ...)` and the scheduler burns STRONG-tier LLM calls with zero yield on the accounts M3.5 is being built to harvest.

6. **Carousel multi-slide LLM turn (K=3) partially supersedes ADR 0005 §Decision 7 for `GraphSidecar` only.** The `_multimodal_hook` sends up to 3 `input_image` parts per LLM turn when the post is a carousel. Discriminator is `len(image_assets) >= 2` — not the raw instaloader `typename` — so a `GraphSidecar` with a single image behaves like a `GraphImage`. `GraphImage` and `GraphVideo` behaviour unchanged. **Already landed in #65 with a single-line marker on §D7.**
   - **Rejected alternative — keep "one image per call".** Real event flyers land on slides 2+ frequently; the single-image discipline systematically misses them, which is the failure mode the scheduler is being built to fix. Cost delta is bounded — 3× image tokens on carousels only, not other shapes — and `MAX_STEPS=8` still bounds the outer loop.

7. **Reel deep-extraction partially supersedes ADR 0006 §Decision 4 for the multimodal-hook video-download surface.** URL-only `MediaAsset` at the ADAPTER remains: `sources/instagram/client.py` does not download binary content. The EXTRACTOR's `_multimodal_hook` downloads the reel `video_url` inside `extract_reel_frames`, materializes `MAX_REEL_FRAMES=3` evenly-spaced JPEG frames via ffmpeg, and sends them as base64 `input_image` parts alongside the thumbnail. **Already landed via [ADR 0013](0013-extractor-side-frame-extraction.md) (#66) with a partial-supersede marker on §D4.**
   - **Rejected alternative — keep the adapter as the binary-download boundary (download to disk in `sources/instagram/client.py` and add a `frames_dir` field to `MediaAsset`).** Forces the adapter to know about downstream LLM concerns (which frames? which encoding? which count?) — a boundary violation.
   - **Rejected alternative — Zen `input_video` passthrough (Path B in the ADR 0013 investigation).** Step 0 probe against `STRONG` returned `400 invalid_request_error` across three shape variants of the `input_video` content part; the API does not accept the type at this Zen version.

8. **Audit log — separate `var/scheduler_runs.jsonl` file for scheduler ticks.** One JSON object per account per tick with fields: `run_id, account_url, posts_discovered, posts_extracted_ok, posts_extracted_error, posts_skipped_idempotent, errors: list[str]`. Append-only, human-tailable.

   **§Decision 8 partially superseded by ADR 0014 (#67) — one JSONL record per source-URL processed (not per account per tick); the record adds `source_kind`, `backend`, `started_at`, `ended_at`, and `gate_reason` for operator observability, and `errors` entries are regex-locked to `<error_type>: <detail>` via `format_error_entry`.**
   - **Rejected alternative — join scheduler records into `var/extraction_runs.jsonl`.** Scheduler "attempt" records have no LLM turns and no `RunStep`-shaped tool dispatches; forcing them through `RunStep` either (a) leaves half the fields empty on every scheduler row, or (b) grows `RunStep` with scheduler-only fields, which the monitor's join-by-`run_id` would then have to case-split around. Schemas diverge — separate files is the cleaner boundary.

9. **Failure handling — `scan_state.consecutive_failures >= 3` skips the account for one tick.** After three consecutive failed ticks against the same account, the scheduler skips that account on the next tick and resets the counter on the tick after that (so a permanently broken account gets exactly one attempt per two ticks worth of interval).
   - **Rejected alternative — exponential backoff with jitter.** More sophisticated failure semantics (jittered delays, per-account cool-down windows, dead-letter queue) belong in a scale-up ADR. MVP simplicity — a hard-count skip is greppable, testable, and easy to reason about; adaptive backoff can be added later if soft-ban dynamics from Meta warrant it.

## Consequences

### Positive

- **Unattended growth of the event catalog covering all three Instagram media shapes.** Static posts (M3 status quo), curator carousels (up to 3 slides visible to the LLM per fetch, 0..N events persisted per post), and text-on-video reels (3 evenly-spaced frames + thumbnail visible to the LLM) — every account in `data/sources.yaml` gets walked on its cadence with no operator intervention.
- **Idempotency is enforced at two layers.** The scheduler's `events_exist_for_source_url` pre-check keeps STRONG-tier LLM budget off already-persisted URLs; the `events` table's composite `UNIQUE(source_url, event_index_in_post)` catches any pre-check miss (race condition, corrupted `scan_state`, manual re-tick) and returns `save_event → duplicate_event` on the LLM's retry. Neither layer is load-bearing alone; together they close the double-persistence window without operator-side reconciliation.
- **Per-account cadence gating via `next_run_after` moves from unused-since-M3 to load-bearing.** The primitive shipped with M2 for a scheduler that hadn't been built yet; M3.5 wires it up as the real gate between "cadence says ready" and "actually dispatch".

### Negative / accepted trade-offs

- **Cron host dependency.** The extractor host must have a cron entry pointing at `planazo-scheduler --tick`. Documented in [`README-package.md`](../../README-package.md) (host setup) and [`AGENTS.md`](../../AGENTS.md) § Setup & Commands (the extractor's runtime prerequisites already name `ffmpeg`; the cron entry joins that list). Failure mode: cron is down → catalog stops growing; monitor's next `data/monitor/YYYY-MM-DD.md` shows zero scheduler runs.
- **No in-tree observability of scheduler ticks.** `var/scheduler_runs.jsonl` is human-tailable but not surfaced anywhere except the file itself. An operator wanting "was last night's tick healthy?" tails the file; a dashboard is deferred to post-MVP.
- **Larger Docker image if reel extraction ever moves into a container.** M3.5 runs host-cron and needs no container change. When a future scheduler container is introduced (ADR 0011-a candidate), its Dockerfile must install `ffmpeg` (~50 MB image growth) so `extract_reel_frames` works inside the container — see [ADR 0013](0013-extractor-side-frame-extraction.md) § Follow-ups.

### Follow-ups

- **Docker-service mode (ADR 0011-a candidate).** When account-list size or tick frequency outgrows single-machine cron, an APScheduler-in-Docker service with restart policy + health check supersedes the cron dispatch mode. Blocked on: real load numbers from a few weeks of production ticks.
- **Whisper transcription for spoken-only reels.** Deferred in [ADR 0013](0013-extractor-side-frame-extraction.md); reels that carry title/date/venue as spoken audio rather than on-screen text are the residual failure mode after ADR 0013's frame path lands. Filed as a separate ticket at merge.
- **Adaptive carousel-K.** Measure slide-2+ event-hit rate over 100 real carousels; if slides beyond the first three yield events, bump the K to 4 or 5. If slides 2-3 hit rate is near-zero, drop K back to 1 and reclaim the image-token budget.
- **Retry sophistication.** Jittered exponential backoff, per-account cool-down, dead-letter queue for repeatedly failing URLs. Blocked on: production evidence that the hard-count skip is inadequate.

## Relates to

- [ADR 0003](0003-sqlite-domain-store.md) — SQLite domain store. `events_exist_for_source_url` (the scheduler's pre-check primitive) reuses the composite `UNIQUE(source_url, event_index_in_post)` index carved out by ADR 0012.
- [ADR 0004](0004-three-store-memory-model.md) — three-store memory model. The scheduler's system user (`telegram_user_id="system"`) satisfies the "every extraction pinned to a user identity" discipline the memory tools' closure binding depends on.
- [ADR 0005](0005-multi-agent-shape.md) — multi-agent shape. §Decision 7 partially superseded for `GraphSidecar` by #65; §Decision 10 superseded outright by [ADR 0012](0012-multi-event-extraction.md); §Decision 11's invariant clause partially superseded by [ADR 0012](0012-multi-event-extraction.md).
- [ADR 0006](0006-instagram-extraction-approach.md) — Instagram extraction approach. §Decision 4 partially superseded for the multimodal-hook video-download surface by [ADR 0013](0013-extractor-side-frame-extraction.md); the adapter still emits URL-only `MediaAsset` entries.
- [ADR 0012](0012-multi-event-extraction.md) — multi-event extraction (#64).
- [ADR 0013](0013-extractor-side-frame-extraction.md) — extractor-side frame extraction (#66).
