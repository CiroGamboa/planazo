# 0006 — Instagram extraction approach

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Relates to:** [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (bounded contexts; this ADR settles the `discovery/` placeholder name as `sources/`), [`0010-extensibility-interfaces.md`](0010-extensibility-interfaces.md) (the `EventSource`/`RawPost`/`MediaAsset` Protocols this ADR conforms to), [`../MVP-ARCHITECTURE.md`](../MVP-ARCHITECTURE.md#5-sources--connectors-srcplanazosources).

## Context

M2 (#16) lands the first concrete `EventSource` adapter: a config-driven Instagram post fetcher that returns validated `RawPost` payloads for static posts, carousels, reels, and video posts, or a typed error branch when a specific post or media type cannot be resolved. The adapter runs in its own Docker service so scraping fragility does not touch the local machine's Python version, cookie state, or IP hygiene.

ADR 0010 already shipped the swap-seam Protocols in `src/planazo/interfaces/sources.py`; ADR 0008 reserved a folder for source adapters under `planazo/` (the ADR-0008 placeholder name was `discovery/`, but MVP-ARCH §5 and issue #16 already refer to the folder as `sources/`, which this ADR settles for the record — any future reader of ADR 0008 chasing the `discovery/` name lands here for the settlement). What is left to record is the load-bearing implementation choices: scraper library, container base, session/cookie policy, rate-limit envelope, per-media-type strategy, media-asset payload shape, and the argument that the interface generalizes cleanly to future TikTok/YouTube/news adapters.

Seven decisions are locked here. Each names at least one rejected alternative with a reason.

**1. Scraper library — `instaloader >= 4.14`.**
Pure-Python (no browser), works with `python:3.12-slim`, supports single-post fetch by URL for images, sidecars (carousels), and video posts. Reels are `GraphVideo` (same code path as regular video posts). Session cookies come from an `INSTAGRAM_SESSION_ID` env var when present; anonymous fetch when absent (rate-limited faster; some post types return `auth_failed`).

*Alternatives rejected:*
- **Playwright/Chromium.** ~1 GB image, no upside for static+video-URL extraction — we do not need to render pages, only read metadata + URLs.
- **`instagrapi`.** Mobile-API impersonation → higher block risk from Meta; instaloader stays inside the public-graph surface.
- **Third-party scraper resellers.** Paid, out of MVP scope, and moves a fragility problem into a vendor-lock-in problem.

**2. Container base — `python:3.12-slim`.**
Follows from (1). Instaloader is pure-Python plus a few C-extension deps that ship as wheels on manylinux, so slim is sufficient.

*Alternatives rejected:*
- **`python:3.12` full.** ~1 GB image, no upside for a pure-Python + wheel stack.
- **`python:3.12-alpine`.** musl-libc breaks or forces source-compile for some wheel deps — real pain for a fragility-sensitive image.
- **`distroless`.** No shell → cannot `docker exec` into a running container to inspect a scraper break, which is exactly the debug flow we need when Meta changes markup mid-day.
- **Playwright's `mcr.microsoft.com/playwright/python`.** Adds an unused browser + ~1 GB weight.

**3. Config shape — one `data/sources.yaml`.**
One committed file describing many sources; today only `instagram:` is populated. Per-source `default_cadence` + `default_media_types`, list of `accounts` with optional per-account `cadence` + `media_types` overrides. Malformed YAML → Pydantic `ValidationError` at `load_config()` time, before any fetch runs.

*Alternative rejected:*
- **Per-source YAML files under `data/sources/*.yaml`.** Cheaper to open one file per source but fragments configuration across multiple files right when the design goal is "one shape, N sources".

**4. Media-type strategy — `MediaAsset.url` only, no binary download in the adapter.**
Each `MediaAsset` carries the URL only — the adapter does not download binary content. The Extractor (M3) fetches media on demand when it needs to pass an image or video to the multimodal LLM. Per-post-type mapping:

- `static_posts` — instaloader `Post.typename == "GraphImage"` → one `MediaAsset(kind="image")`. Must work.
- `carousels` — `Post.typename == "GraphSidecar"` → one `MediaAsset(kind="image"|"video")` per sidecar node.
- `reels` and `video_posts` — `Post.typename == "GraphVideo"` (both map to the same shape) → `MediaAsset(kind="video", url=video_url, duration_seconds=video_duration)` + `MediaAsset(kind="thumbnail", url=display_url)`. If the video URL cannot be resolved (login-walled, expired signed URL) → typed `unsupported_media` branch naming the reason.
- Any post type outside the four supported kinds → typed `unsupported_media`.

*Alternatives rejected:*
- **Fetch-and-embed binary in `MediaAsset`.** Inflates `RawPost` size, forces a serialization decision (base64? separate blob field?) the schema does not need today.
- **Re-fetch from the CDN inside the Extractor.** Instagram's CDN URLs are signed and can expire between fetch and parse; the URL alone is not always sufficient. The safer contract in a follow-up is `MediaAsset.url` is the last-known-good URL; if it 404s at Extractor time, the Extractor retries via the adapter's `refresh(shortcode)` (deferred — see Follow-ups).
- **Proxy through object storage.** Adds an S3/blob-store dependency the MVP explicitly avoids.

**5. Cookie/session — `INSTAGRAM_SESSION_ID` env var.**
Threaded into the container via Compose env. Owner sets it once locally by inspecting a logged-in browser session; container reads it at boot and hands it to `instaloader.Instaloader.load_session_from_cookies`. Absent → anonymous mode → `auth_failed` return for private/reel/video routes that require login. No cookie files, no mounted volumes for session state.

*Alternatives rejected:*
- **Mounted `.session` file.** Puts host-filesystem session state under bind-mount, needs `.gitignore` discipline, filesystem-permissions care, and a "how to bootstrap on a fresh machine" recipe. Env var is a strictly-smaller surface.
- **Session-refresh worker.** Auto-refresh via a headless login flow is a whole subsystem the MVP does not need; owner refreshes manually when the value 401s.
- **Anonymous-only.** Fails too many post types (reels, videos, sometimes even public carousels) to be a viable default.
- **Third-party session provider.** External paid dependency, out of scope.

**6. Rate-limit envelope — surface, do not retry.**
Per-account cadence (default 6h) enforced by the future scheduler (out of scope), not by the adapter. The adapter itself surfaces `rate_limited` when instaloader raises its 429-shaped exception (`TooManyRequestsException` in `instaloader==4.15.3`); it never retries internally. Retry policy belongs to whoever calls the adapter.

*Alternatives rejected:*
- **Exponential-backoff retry inside the adapter.** A `fetch_post` call that blocks for `2^n * base` seconds while sleeping through a retry loop is worse than a fast typed error the scheduler can respect; also gives Meta more requests than a single scheduled cadence would.
- **Circuit-breaker with cool-down.** The state (open/closed/half-open) belongs to whichever entity coordinates fetches across accounts — the scheduler, not the adapter.
- **External queue with retry orchestration.** No queue in v1; adding one is a full follow-up ADR.

**7. Live test URL discipline.**
Exactly one `@pytest.mark.live` test targeting a hardcoded public Barcelona-venue static post URL. The URL is documented in the test docstring so a future breakage is traceable; if Meta removes the post the test fails loudly and the URL is refreshed in a follow-up. Reels/videos are not exercised live — their acceptance bar is exploratory per the ticket.

*Alternative rejected:*
- **Live tests across every media type.** Multiplies the flake surface and the chance of a mid-CI 429 blocking unrelated PRs; the container build + `docker compose config` cover the "does the wiring work" question, and the static-post live test covers "does the container actually reach Instagram".

**Generalized-protocol argument.**
`EventSource`/`RawPost`/`MediaAsset` shape is source-agnostic. A future TikTok adapter is `class TikTokSource:` with the same three attributes (`name`, `cadence`, `fetch_post`) and its own container under `docker/sources-tiktok.Dockerfile`. `data/sources.yaml` grows a `tiktok:` block; no interface change. Same story for YouTube, news pages, Meetup (ADR 0011, conditional), Eventbrite (ADR 0011, conditional). The seven decisions above are Instagram-specific implementation details behind a source-agnostic seam.

## Decision

Planazo's Instagram source adapter is a Dockerized Python service that uses `instaloader >= 4.14` on a `python:3.12-slim` base, authenticates via an `INSTAGRAM_SESSION_ID` env var (anonymous fallback), reads its target-account list + cadence + per-account media-type flags from a Pydantic-validated `data/sources.yaml`, and returns `RawPost` (with URL-only `MediaAsset` entries — no binary download) or one of five typed error branches — `unsupported_source`, `rate_limited`, `auth_failed`, `not_found`, `unsupported_media` — from every call. Rate-limit backoff is surfaced to the caller, not retried internally. The adapter conforms structurally to `interfaces.sources.EventSource`; the same seam accepts any future TikTok / YouTube / news / Meetup / Eventbrite adapter without a Protocol change.

## Consequences

### Positive

- **Fragility is contained in one swap point.** `sources/instagram/client.py` wraps every `instaloader` call. If Meta breaks instaloader mid-M2/M3, we cut a follow-up ADR (0006-a) and swap the scraper without touching the `EventSource` contract.
- **The Extractor never sees session state.** Cookies live in the container's env, not in Python memory reachable by the agent loop.
- **Config errors fail fast, not mid-fetch.** A typo in `data/sources.yaml` raises `ValidationError` at boot, before any HTTP call.
- **The interface generalizes.** A TikTok adapter is a new folder + a new Compose service + a new `sources.yaml` block. Zero Protocol changes.
- **URL-only `MediaAsset` keeps `RawPost` small.** The Extractor pulls binary content only when it decides an image or video needs LLM inspection.

### Negative / accepted trade-offs

- **Session cookies are manual.** Owner refreshes `INSTAGRAM_SESSION_ID` when it 401s. Anonymous fallback covers static public posts but not reels/videos consistently. Auto-refresh is a follow-up ticket.
- **Instaloader is single-maintainer.** Version pin (`>=4.14`, upper bound in Stage 2) plus the `client.py` swap point is the mitigation.
- **Anonymous rate-limiting is real and fast.** The live test is `@pytest.mark.live` (opt-in) to keep it out of the CI happy path.
- **CDN URL expiry.** `MediaAsset.url` may 404 by the time the Extractor fetches it. Follow-up ticket adds `refresh(shortcode)`; MVP acceptance bar is "extract quickly after fetch".
- **`GraphSidecar` mixed-media coverage is thin.** Instaloader's `sidecar_nodes` iterator behavior across image+video mixes is documented but not battle-tested against every carousel shape; live coverage is deferred to M3 exercising the surface end-to-end.

### Follow-ups

- **Scheduler for source adapters.** A future ticket consumes `next_run_after()` + the per-account cadence in `data/sources.yaml` and actually runs the adapter on a clock (cron / GHA / worker — choice TBD).
- **CI Docker build check for `docker/sources-instagram.Dockerfile`.** A GitHub Actions job that builds the image on every PR touching `docker/` or `src/planazo/sources/`. Deferred out of this ticket to keep CI unchanged.
- **`refresh(shortcode)` on the adapter.** When a `MediaAsset.url` 404s at Extractor time, the Extractor retries via the adapter to get a fresh signed URL.
- **`.local.yaml` override pattern.** If sensitive account URLs (private venues, invite-only groups) ever need to live here. Deferred: today all accounts are public venues.
- **ADR 0011 — Meetup / Eventbrite adapters.** Conditional on either shipping past POC. Same `EventSource` seam.
- **ADR 0006-a — scraper swap.** If instaloader breaks mid-M2/M3, this ADR is superseded by a follow-up that names the replacement (Playwright, instagrapi, third-party) without touching the `EventSource` contract.
