# ADR 0014 — Instagram discovery: two backends + scheduler-side routing

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Landed by:** M3.5 (#67)
- **Relates to:** [`0006-instagram-extraction-approach.md`](0006-instagram-extraction-approach.md) (adapter contract stays URL-only), [`0011-scheduled-ingestion.md`](0011-scheduled-ingestion.md) (partial-supersede markers below)

## Context

M3.5's scheduler (#67) needs a discovery primitive that maps an Instagram account URL to the last N post URLs so the extractor can iterate them. ADR 0011's Context sketched it as a `list_recent_posts(account_url) -> list[PostRef]` primitive on `InstagramSource`, backed by an authenticated `instagrapi` session on a burner account (`#63`). Two facts landed after ADR 0011 that force a re-shape:

- **`#63` slid post-M3.5.** Meta's appeal rejected the burner-account API logins with `BadPassword`. No authenticated discovery path is available on the M3.5 timeline.
- **Two anonymous discovery paths exist, with different reach.** A `curl_cffi + web_profile_info` probe extracted 12 shortcodes per account for four creator/curator accounts (`@bcn.agenda`, `@curated.agenda`, `@planbcity`, `@feverbcn`) — the anonymous endpoint works for creator content but returns HTTP 400 with a `laser.provider` schema block for business venue accounts (`@sala_apolo`, `@razzmatazzclubs`). A separate paid HikerAPI probe extracted 9 shortcodes each for those two blocked accounts, and a full end-to-end HikerAPI → `extract_once` run persisted 10 events for `sala_apolo`. Every account M3.5 targets is reachable via one of the two paths; neither path alone covers the whole target set.

Discovery and extraction are two different responsibilities against two different rate-limit envelopes. HikerAPI charges per request against a paid quota; Meta's `web_profile_info` runs against a soft-ban heuristic keyed on TLS fingerprints. Coupling them on `InstagramSource` would mean every consumer of the adapter (the ad-hoc CLI, tests, future single-post fetches) transitively pulls in HikerAPI + `curl_cffi` even if all they want is a single `Post.from_shortcode` fetch.

## Decision

Planazo ships **two discovery clients** and routes between them at the scheduler composition root via a new `AccountConfig.backend: Literal["anonymous", "hikerapi"] = "anonymous"` field on `data/sources.yaml`. Both clients implement a shared `InstagramDiscoveryProtocol` (`list_recent_posts(account_url, limit) -> list[str]`) and live under `src/planazo/sources/instagram/`. `InstagramSource` stays exactly as it is today — no `list_recent_posts` on the adapter, no HikerAPI or `curl_cffi` import from adapter code. The scheduler (`src/planazo/scheduler/service.py`) holds `dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol]` and picks by `account.backend` at tick time.

`HikerClient` runs a **random-selection multi-key pool with a 5-minute retirement window**. Keys come from both the singular `PLANAZO_IG_HIKER_API_KEY` env var and every `PLANAZO_IG_HIKER_API_KEY_*` env var (numbered peers, not fallbacks) via `HikerClient.from_env()`; values are de-duplicated across the pool so the same secret set under two env-var names appears exactly once. Every request draws one key via `random.choice(available_keys)`; a 401/403/429 retires the drawing key for `RETIREMENT_WINDOW = timedelta(minutes=5)` in-process and retries with a fresh draw, capped at pool size. When every key is retired the client raises `HikerClientError("rate_limited", "all N hikerapi keys retired; next available at <iso-ts>")` naming the earliest expiry.

`AnonInstagramClient` runs `curl_cffi.requests.Session(impersonate="chrome")` against `https://www.instagram.com/api/v1/users/web_profile_info/?username={u}` with `x-ig-app-id: 936619743392459`. Business venue accounts surface as `AnonInstagramClientError("unsupported_media", …)` when Meta's body includes `laser.provider` or `ig_business_category_subvertical` — the operator's routing signal to flip the account's `backend` to `"hikerapi"`.

Both clients are I/O-only. They return `list[str]` of canonical post URLs — never a `RawPost`, never a caption, never anything the LLM could interpret as instructions (Rule 2 trust boundary).

### Alternatives rejected

- **Grow `InstagramSource` with `list_recent_posts(account_url, limit) -> list[str]`.** Rejected: discovery and extraction are two different responsibilities against two different rate-limit envelopes; coupling them on the adapter pulls HikerAPI + `curl_cffi` into every downstream consumer transitively. Also violates the "adapter never grows" discipline #66 / ADR 0013 already respected (reel frame extraction lives in `extraction/frames.py`, not on the adapter). Isolation is cheaper at the composition root.
- **First-viable-key with sticky retry** (multi-key pool strategy). Rejected: risks concentrating traffic on one key until Meta flags it. Once a key is "the sticky key" it takes the majority of requests; a bot-detection heuristic keyed on per-key request rate flags it faster than a uniformly-distributed pool.
- **Round-robin key selection.** Rejected: creates predictable inter-request timing patterns Meta could fingerprint over long horizons (`key_i` seen every N requests → the interval acts as a per-key beacon).
- **No retirement window — retry the same key immediately on 429.** Rejected: a 429 means "wait," not "try again in a millisecond." Immediately re-drawing the same rate-limited key wastes an HTTP round-trip and produces no signal.
- **`PLANAZO_IG_HIKER_API_KEY` as a fallback (used only when no `_*` numbered keys are present).** Rejected in favor of additive semantics: both env-var families are peers, de-duplicated by value. Fallback semantics would silently drop the singular when an operator added a numbered key alongside it — a footgun during ops migrations.
- **Wait for `#63` (`instagrapi` burner) to come back and skip anonymous discovery.** Rejected: Meta's appeal rejected the API logins and the timeline is uncertain. Anonymous + HikerAPI cover the full M3.5 target set today; the burner can slot in as a third `InstagramDiscoveryProtocol` implementation later.

## Consequences

### Positive

- **Every M3.5 target account is reachable.** Creator/curator accounts route through the free anonymous backend; blocked business-venue accounts route through the paid HikerAPI backend. No account is stuck waiting for `#63`.
- **`InstagramSource` stays honest.** No new adapter method, no transitive HikerAPI or `curl_cffi` import for consumers who only want a single `Post.from_shortcode` fetch. The scheduler owns discovery routing at the composition root — the shape #66 / ADR 0013 already established.
- **HikerAPI cost distributes uniformly across the pool.** Random selection load-balances traffic so no single key concentrates enough traffic to trip a per-key rate limit within one live run. Adding capacity is one env-var line.
- **Every failure branch is a typed error.** `HikerClientError` and `AnonInstagramClientError` both carry the shared `ErrorType` taxonomy from `sources/base.py`; the scheduler's per-URL `SchedulerRunRecord.errors` field logs each failure via `format_error_entry` — no free-form strings, no exception traceback bytes.

### Negative / accepted trade-offs

- **`curl_cffi` version pinning is load-bearing.** The Chrome-impersonate TLS fingerprint is the anti-detection lever; Meta may recognize and block a specific `curl_cffi` version's fingerprint. Pinned `>=0.7,<1.0` with a documented bump-on-failure recipe in the operator playbook.
- **Meta may rotate `x-ig-app-id` and the `edge_owner_to_timeline_media` path.** Historically every 2-4 weeks. The `AnonInstagramClient` maps a missing edges path to `not_found` so the rotation surfaces as a typed error in `var/scheduler_runs.jsonl` — the operator's mitigation is a constant-swap + test-update, not a re-architecture.
- **HikerAPI key exhaustion during a live run.** With a 100-request/month free-tier and each account tick costing ~2 requests (username→user_id, user_id→medias), a 3-account tick every 6h burns ~360 requests/month per key. The pool + retirement window absorb the burst; sustained load requires paid tier.
- **Scheduler-side routing means both clients live inside the same PR.** The composition root gets both imports even if one backend is never configured. Acceptable — the total code footprint of the two clients is ~500 lines each.

### Follow-ups

- **Stage 3 flips this ADR `Status: Proposed` → `Status: Accepted`** in the same commit as `src/planazo/scheduler/service.py`, which is the first module that acts on the routing decision.
- **Storage-migration framework** — flagged by ADR 0011's follow-ups. Not blocking Stage 1.
- **A third backend when `#63` (`instagrapi` burner) comes back.** New `InstagramDiscoveryProtocol` implementation dropped into the `backends` dict; the config gains `Literal["anonymous", "hikerapi", "instagrapi"]`. No scheduler changes.

### Status markers

- **ADR 0011 Context claim partially superseded by ADR 0014 (#67) — the `list_recent_posts` discovery primitive lives in the `scheduler/` bounded context, not on `InstagramSource`.**
- **ADR 0011 §Decision 3 partially superseded by ADR 0014 (#67) — `scan_state` primary key renamed from `account_url` to `source_url` because posts and accounts share the state table and `source_url` is the honest name for both entry kinds.**
- **ADR 0011 §Decision 8 partially superseded by ADR 0014 (#67) — audit-log field set expands with `source_kind` (discriminator), `backend` (attribution for account entries), `started_at`, and `ended_at` (operator observability); grain changes from "one JSON object per account per tick" to "one per source-URL processed" because both posts and accounts need per-URL attribution.**
