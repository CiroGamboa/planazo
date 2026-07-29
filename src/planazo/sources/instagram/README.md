# Instagram source adapter

This module implements the Instagram end of Planazo's source-adapter contract. It exposes:

- **`InstagramSource`** (`adapter.py`) — `fetch_post(url)` returns a `RawPost` (media-type-agnostic Pydantic shape) or a typed error dict, wired into the Extractor via the `fetch_instagram_post` LLM tool.
- **`AnonInstagramClient`** (`anon_client.py`) — anonymous discovery + fetch via `curl_cffi` against Meta's `web_profile_info` endpoint. Free but rate-limited; refused by business-account accounts.
- **`HikerClient`** (`hiker_client.py`) — paid multi-key HikerAPI-backed discovery + fetch. Handles business-account accounts the anonymous endpoint refuses; ~$0.03/request; multi-key pool with 5-minute retirement on 401/403/429.
- **`NarrativeLogger`** (`narrative.py`) — opt-in stdout observer for `planazo-scheduler --once --verbose`; prints one structural line per extraction phase, layered on top of the JSONL sidecar.

The Extractor ([`../../agents/extractor.py`](../../agents/extractor.py)) is the single consumer of the LLM tool the adapter registers; the Recommender never touches captions or the raw post shape (ADR 0005 §Trust boundary).

## Quick demo

Three commands, ready to copy-paste:

```bash
# 1. Extract a single post (verbose narrative to stdout).
uv run planazo-scheduler --once https://www.instagram.com/p/DbSiUpoDNiZ/ --verbose

# 2. Run one scheduler tick over every entry in data/sources.yaml.
uv run planazo-scheduler --tick

# 3. Config-only smoke: validate data/sources.yaml (no LLM cost, no network).
uv run python -c "from planazo.sources.config import load_config; print(load_config())"
```

Command 1 is the live-demo command. It goes through the full pipeline (fetch → multimodal LLM turn → `save_event` or `report_extraction_status`) and prints step-by-step narrative to stdout. See ADR 0017 for the narrative log discipline.

Command 2 is the cron-driven ingestion path. It walks `posts:` + `accounts:` in `data/sources.yaml`, respects the per-URL cadence gate, and appends one `SchedulerRunRecord` line per URL to `var/scheduler_runs.jsonl`.

Command 3 is the offline smoke — a Pydantic parse of the config file, no network, no LLM. Useful before pushing a config change.

## Config: `data/sources.yaml`

Minimal example with one anonymous account, one HikerAPI-backed account, and one explicit post URL:

```yaml
sources:
  instagram:
    default_cadence: "6h"
    default_media_types:
      static_posts: true
      reels: true
      carousels: true
      video_posts: true

    accounts:
      - url: "https://instagram.com/example_creator"
        cadence: "24h"
        # backend: "anonymous"  — inherited default
      - url: "https://instagram.com/example_business_venue"
        cadence: "24h"
        backend: "hikerapi"

    posts:
      - url: "https://www.instagram.com/p/DbSiUpoDNiZ/"
```

`SourcesConfig` (from `sources/config.py`) parses this at load time — a malformed file fails boot with `ValidationError` before any network call happens. `posts:` URLs are validated against `instagram.com/{p|reel}/<shortcode>/`; an account URL pasted into `posts:` raises at load time, not runtime.

## Backend selection: `anonymous` vs `hikerapi`

| Backend       | When to pick                                                                                                                             | Cost                | Rate limit                                          |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------- | --------------------------------------------------- |
| `anonymous`   | Creator accounts. Free. Uses `curl_cffi` + Meta's `web_profile_info`.                                                                    | Free                | ~200 req/hour per IP; occasional 429 during bursts. |
| `hikerapi`    | Business accounts (e.g. venue/club/promoter accounts). The anonymous endpoint returns 400 for these; HikerAPI is the only working path.  | ~$0.03/request       | Multi-key pool retirees 401/403/429 keys for 5 min. |

`AccountConfig.backend` defaults to `anonymous`; set `backend: "hikerapi"` on any account whose anonymous fetch returns `not_found` or `auth_failed`. See [ADR 0014](../../../docs/adr/0014-instagram-discovery-backends.md) for the split rationale.

## What to expect

Sample `--verbose` output shape (real URLs and timestamps redacted; the structural shape is stable):

```
[14:23:41] Fetching post DbSiUpoDNiZ from Instagram...
[14:23:43] Fetched post - 3 media asset(s)
[14:23:52] Saved event at index 0 - category=music, confidence=0.87
[14:23:53] Saved event at index 1 - category=music, confidence=0.82
[14:23:54] Loop terminated: stopped=answered, steps=3
{"source_url":"...","source_kind":"post","backend":"anonymous","gate_reason":"first_run",...}
```

The last line is the `SchedulerRunRecord` — always emitted, `--verbose` or not. The `[HH:MM:SS]` prefixed lines are the narrative surface and disappear when `--verbose` is omitted.

**Rule 2 discipline (ADR 0017).** The narrative log carries only URLs, shortcodes, integer counts, floats, and Literal-valued fields (`category`, `status`, `error_type`, `stopped`). It NEVER interpolates captions, event titles, event descriptions, `LoopResult.answer`, `LLMDecision.rationale`, or `report_extraction_status(notes=...)`. Full sanitized text lives in `agent_runs.user_query` / `llm_decisions.rationale` inside the DB trust boundary; the demo transcript stays structural.

## Cost expectations

Per URL:

- **`anonymous` fetch:** free. One HTTPS call to `web_profile_info` (~250 KB response).
- **`hikerapi` fetch:** ~$0.03 per URL, drawn from the multi-key pool with random selection.
- **Reel frame extraction:** free (host `ffmpeg`). CPU-bounded; ~200-800ms per reel.
- **LLM extraction turn:** `STRONG` tier (`gpt-5.4-strong`, ~$0.02-0.08 per URL for a single-image or short-carousel post; up to $0.10 for a reel that sends 3 frames + thumbnail).

Per `--tick`:

- Anonymous account: one `web_profile_info` call for discovery + up to 12 `Post.from_shortcode` calls for shortcode metadata. Idempotency pre-check skips URLs already persisted.
- HikerAPI account: one `/gql/user/medias/chunk_v2` call (~$0.03) + N per-shortcode calls (~$0.03 each). Same idempotency pre-check gates LLM cost.
- LLM cost is bounded by the count of NEW URLs (never-persisted) surviving the pre-check.

For a typical demo tick (5 URLs, 3 new): expect ~$0.15-0.40 total.

## Troubleshooting

- **HikerAPI 401/403.** A key retired for the last 5 minutes. Wait or refresh a pool member: `PLANAZO_IG_HIKER_API_KEY_1`, `_2`, ... — every distinct env var value becomes a pool member. See [ADR 0014](../../../docs/adr/0014-instagram-discovery-backends.md).
- **`web_profile_info` returns HTTP 400 with an anonymous account.** The account is a business account. Switch `backend: "hikerapi"` on that `AccountConfig` and retry.
- **`fetch_post` returns `error_type: "auth_failed"` for a public post.** Anonymous rate limits sometimes escalate to auth challenges. Copy `sessionid` from a logged-in browser and paste into `.env` as `INSTAGRAM_SESSION_ID=<value>`; the anonymous client picks it up.
- **`ffmpeg: command not found` during a reel extraction.** The Extractor needs `ffmpeg` on the host `PATH`. `brew install ffmpeg` on macOS, `apt-get install ffmpeg` on Linux. The scheduler docker image already ships ffmpeg — this only affects the host that runs `planazo-agent` / `planazo-scheduler`.
- **`save_event_failed` on a fresh dev DB.** The migration framework applies on `db.connect()`; a stale `var/planazo.db` from before the M3.6 migrations lands is expected to fail. Delete `var/planazo.db` and re-run.
- **LLM cost climbs unexpectedly.** Run `sqlite3 var/planazo.db "SELECT source_url FROM events WHERE ingested_at > datetime('now', '-1 day')"` to confirm the day's persisted set. New URLs above the idempotency pre-check burn LLM budget; already-persisted URLs short-circuit.

## Related docs

- [ADR 0006 — Instagram extraction approach](../../../docs/adr/0006-instagram-extraction-approach.md) — scraper choice, container base, per-media-type fallback rules.
- [ADR 0011 — Scheduled ingestion](../../../docs/adr/0011-scheduled-ingestion.md) — `planazo-scheduler` shape, `SchedulerRunRecord`, cadence gate.
- [ADR 0013 — Extractor-side frame extraction](../../../docs/adr/0013-extractor-side-frame-extraction.md) — ffmpeg-based reel frame sampling.
- [ADR 0014 — Instagram discovery backends](../../../docs/adr/0014-instagram-discovery-backends.md) — `anonymous` vs `hikerapi` split, HikerAPI key pool.
- [ADR 0017 — Instagram demo narrative logs](../../../docs/adr/0017-instagram-demo-narrative-logs.md) — the `--verbose` narrative log's Rule 2 discipline and observer wiring.
