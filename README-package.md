# Planazo Agent

The hand-rolled observe -> reason -> act -> verify agent that powers Planazo's event discovery: it calls source/extraction tools, persists normalized event candidates, ranks them, and — only after explicit chat approval — creates the user's calendar entry.

No agent framework, no API server, no frontend here (see `AGENTS.md` rule 5 and `docs/adr/`) — just the loop, the tool registry, and the guardrails, all plain Python.

## Quick start

```bash
uv sync                                                                  # install
uv run planazo-agent --calendar "save a tech event evt-1 called AI Meetup at 2026-08-01T19:00:00 in Barcelona, confidence 0.9"  # one-shot
uv run planazo-agent                                                     # interactive REPL
uv run planazo-monitor --since 24h                                       # grade recent runs
uv run planazo-monitor --dry-run                                         # grade canned seed runs
uv run planazo-monitor --dry-run --run-id seed-injection-near-miss       # one-call monitor smoke test
uv run planazo-scheduler --tick                                          # one scheduled-ingestion tick (host-cron entry point)
```

`agentlib` (the LLM wrapper) needs `OPENCODE_API_KEY` set in a `.env` file at the repo root; copy `../.env.example`. If the key is unset, the CLI prints one actionable line and exits without calling the provider; if the key is present but invalid or the provider errors, it prints a single-line message — never a traceback.

In the REPL, type a prompt at the `agent> ` indicator; `exit`/`quit`, EOF (Ctrl-D), or Ctrl-C ends the session. Each line runs independently — no history carries across turns.

Options:

- `--strong` and `--model {cheap,strong}` select the model role and are mutually exclusive; passing both is a usage error. The default is the cheap role.
- `--max-steps N` caps the loop's steps (`N` must be >= 1).
- `--calendar` adds the two calendar reference tools to the run's tool set.
- `--user-id N` binds the run to one user (`N` must be >= 1): it adds the four memory tools bound to that id and pushes that user's stored preferences into the system message. It is unauthenticated — whatever id the shell supplies is used — so this CLI is an operator's surface, not a user-facing one ([ADR 0004](../docs/adr/0004-three-store-memory-model.md)).

Output shape: a per-step tool trace (`step N: tool(args) -> result`), then a separated final block with the answer (or a `(no final answer — hit max steps)` notice), the step count, and the stop reason.

For a low-cost live monitor check, use `--dry-run --run-id <id>` to grade exactly one deterministic trace. The available ids are `seed-clean`, `seed-adherence-violation`, and `seed-injection-near-miss`; repeat `--run-id` to select more than one. A full `--dry-run` grades all three.

## Commands

```bash
uv run pytest              # tests (the LLM is mocked; live tests are opt-in, see below)
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy src            # types
```

## Tools

`search_events` is the one tool on every run, whatever the flags. It queries the SQLite domain store at `var/planazo.db` for stored events, filtered by `category`, `city`, and `start_after` (an empty string means "no filter on that field"), and returns `{"events": [...], "total": N}` or a typed `invalid_search_filter` error. Its writing counterpart, `save_event`, lives beside it in `planazo.storage.dao` for the extraction path; the domain store's shape and its two dao tiers are [ADR 0003](../docs/adr/0003-sqlite-domain-store.md).

The other two groups are opt-in: the memory tools with `--user-id N`, the calendar reference tools with `--calendar`.

Every tool returns a typed `error_type` on bad input rather than persisting something partial. A tool that raises anyway (bug, disk error) is caught one layer up, inside `planazo.agents.loop.run_loop`'s dispatch, and fed back to the model as a `tool_failed` marker rather than crashing the run or looking like valid data.

### The memory tools

`--user-id N` (or `run_once(user_id=N)`) adds four tools over the JSON docstore at `var/memory/`, built by `planazo.memory.api.build_memory_tools`:

- **`retrieve_memory(query, scope)`** — facts about the user whose cue overlaps `query`, as `{"facts": [...], "total": N}`.
- **`save_memory(cue, content, scope)`** — one durable fact, filed under `cue` for later recall.
- **`retrieve_notes(event_id, scope)`** — the notes filed against one event, as `{"notes": [...], "total": N}`.
- **`save_note(event_id, content, scope)`** — one free-form note about one event.

`scope` is `private`/`shared` on a write and `private`/`shared`/`both` on a read. **`user_id` is not a parameter of any of them**: each is a closure over the id the caller bound, so it appears in no tool schema and a tool call that supplies one fails outright instead of reading another user's private facts ([ADR 0004](../docs/adr/0004-three-store-memory-model.md)). Bad input comes back as a typed `invalid_memory_data` (writes) or `invalid_memory_query` (reads).

### The query interpreter

`planazo.query.interpret(text)` translates a free-text `/find` message into a validated `planazo.query.models.SearchIntent` via a single Zen `call()` on the CHEAP model with one function-call tool (`_record_search_intent`, whose signature `schema_for` reflects into the tool schema). It is **not a registered tool** — the Recommender's loop never sees it; the bot's `/find` handler (M6) will be its only caller. On any failure — the LLM raises, the reply carries no tool call, the tool name does not match, or Pydantic rejects the wire arguments — `interpret` returns a degraded `SearchIntent` (today -> today+72h, `city="Barcelona"`, empty `categories`) tagged `error_type="interpreter_fallback"` instead of raising or silently defaulting. Callers **must** branch on `error_type` before reading any other field; the two shapes are structurally identical apart from that tag.

### The calendar reference tools

These two are the calendar reference implementation, reachable only via `--calendar` (or `run_once(calendar_enabled=True)`). They are JSON-backed and stay untouched until v0.2's real Google Calendar wiring replaces them; `confirm_and_create_calendar_event` is the only irreversible tool in the tree, so it is also the approval gate's only end-to-end demonstration.

- **`save_event_candidate`** (reversible) — persists one normalized event candidate to `var/event_candidates.json`. Runs without approval; re-saving a correction is just another write. Validates input through `planazo.calendar.EventCandidateInput` and returns a typed `error_type` (`invalid_event_data`, `low_confidence_extraction`) instead of persisting bad or unreliable data.
- **`confirm_and_create_calendar_event`** (irreversible) — looks up a previously saved candidate by `event_id` and creates the calendar entry in `var/calendar_events.json`, optionally emailing invitees. This is visible to a third party, so the CLI gates it behind a terminal `y/N` approval prompt before dispatch; declining feeds the model a `DECLINED_RESULT` marker instead of running the tool. Returns a typed `error_type` (`invalid_confirmation_data`, `missing_invitees`, `event_not_found`) on bad input instead of silently creating a broken entry.

## The loop

`planazo.agents.loop.run_loop` drives `agentlib.tools.call` across turns, dispatching whatever tool calls the model requests and feeding results back, until either:

- the model answers with no further tool calls (`stopped="answered"`, or `"truncated"` if the output cap cut it off) — the done signal, or
- `max_steps` is reached without an answer (`stopped="max_steps"`, `answer=None`) — the explicit backstop.

It is completely generic over `tools`/`registry`, so it has no Planazo-specific code in it; `planazo.agents.event_agent.run_once` is the one place the event-discovery tool set is bound to it.

`run_loop`'s optional `system` argument is prepended once as the run's system message, ahead of the user's. `run_once` is what assembles it — the markdown rules from `data/rules/`, re-read on every call, plus the bound user's preference rows — so that is the run's whole push context. Everything else the model sees, tool results included, arrives as a `function_call_output`, never in the system role.

### Live tests

```bash
uv run pytest -m live tests/test_agents_gate_live.py -v -s              # hits the real LLM; needs a real OPENCODE_API_KEY
uv run pytest -m live tests/test_sources_instagram_live.py -v -s        # fetches one public Barcelona-venue Instagram post
```

## Source adapters

Each source runs in its own Docker container so a Meta break (Instagram) does not affect a TikTok / news / Meetup adapter. `data/sources.yaml` names every source's cadence, per-media-type strategy, and account list; `docker/sources-<name>.Dockerfile` builds the image; `compose.yaml` wires the bind-mount and env vars.

Instagram is the first landed adapter:

```bash
docker compose up sources-instagram                                                 # print resolved fetch plan (no network)
docker compose run --rm sources-instagram --url https://instagram.com/p/ABC/        # fetch one post; RawPost JSON on stdout
INSTAGRAM_SESSION_ID=<value> docker compose run --rm sources-instagram --url <URL>  # same, with a logged-in session cookie
uv run planazo-sources-instagram --dry-run                                          # host-side dry-run (no container)
uv run planazo-sources-instagram --url https://instagram.com/p/ABC/                 # host-side fetch (no container)
```

`docker compose up sources-instagram` defaults to `--dry-run` — it resolves the plan from `data/sources.yaml` and exits 0 without any network calls. Use `docker compose run --rm sources-instagram --url <URL>` to fetch one specific post; `--dry-run` and `--url` are mutually exclusive and exactly one is required per invocation. `INSTAGRAM_SESSION_ID` is optional — copy the `sessionid` cookie from a logged-in Instagram browser session into `.env` when a public-account fetch returns `auth_failed`, otherwise leave it unset. The CLI never writes anywhere; `--url` prints one line of `RawPost.model_dump_json()` (happy path) or the typed error dict — `{"error_type": "…", "message": "…", "url": "…"}` — to stdout. See [ADR 0006 — Instagram extraction approach](docs/adr/0006-instagram-extraction-approach.md).

## Scheduled ingestion

`planazo-scheduler` is the host-cron entry point for periodic ingestion: on every `--tick` it reads `data/sources.yaml`, walks the configured `posts:` + `accounts:` blocks, routes each account through `AccountConfig.backend` to one of two discovery backends (`anonymous` via `curl_cffi` + Meta's `web_profile_info`, or `hikerapi` via the paid multi-key HikerAPI pool), pre-checks discovered URLs against `events_exist_for_source_url` to skip already-persisted URLs, and dispatches survivors into `extract_once` under a seeded system user (`telegram_user_id="system"`). One `SchedulerRunRecord` line per source URL processed lands in `var/scheduler_runs.jsonl` for human tailing. See [ADR 0011 — Scheduled ingestion](docs/adr/0011-scheduled-ingestion.md) and [ADR 0014 — Instagram discovery backends](docs/adr/0014-instagram-discovery-backends.md).

```bash
uv run planazo-scheduler --tick                                       # one-shot tick over sources.yaml
uv run planazo-scheduler --once https://www.instagram.com/p/ABC/      # diagnostic single-URL run (bypasses cadence)
uv run planazo-scheduler --once https://www.instagram.com/reel/ABC/   # same, for a reel URL
uv run planazo-scheduler --once https://www.instagram.com/acct/       # single-account run (must be in sources.yaml)
```

Exit codes: `0` when the tick completed (regardless of per-URL outcomes — operators read the JSONL log for per-URL health); `1` on an uncaught runtime exception; `2` on a config-time failure (malformed `sources.yaml`, missing `PLANAZO_IG_HIKER_API_KEY_*` when a `hikerapi`-backed account is configured, or an `--once <url>` call against an account URL that is not in `sources.yaml`).

`hikerapi` accounts need one or more `PLANAZO_IG_HIKER_API_KEY[_N]` env vars set — every distinct value across the singular `PLANAZO_IG_HIKER_API_KEY` and any numbered `PLANAZO_IG_HIKER_API_KEY_1`, `_2`, ... peers becomes a pool member. On each HikerAPI call the client draws uniformly at random from the non-retired keys; a 401/403/429 retires the drawing key for five minutes and the request retries against a fresh draw.

Wire the tick into cron every 15 minutes (adjust the path and cadence to your host):

```
# macOS: `crontab -e`; Linux: `sudo crontab -e -u planazo`
*/15 * * * * cd /path/to/planazo && /usr/local/bin/uv run planazo-scheduler --tick >> var/scheduler.log 2>&1
```

Notes for the cron entry:

- Use the absolute path to `uv` — cron's `PATH` is minimal and `uv` will not be found via `PATH` lookup unless the crontab is configured with `PATH=` at the top. `which uv` on the host that runs cron gives the path to paste.
- `var/scheduler.log` accumulates cron stdout/stderr; rotate it with `logrotate` (Linux) or `newsyslog` (macOS) if the host runs the tick long enough for the file to matter. `var/scheduler_runs.jsonl` — the per-URL structured audit log — is the primary artifact and is never rotated automatically.
- The default cadence in `sources.yaml` is 6 hours per account; a 15-minute cron interval means most ticks are no-ops that short-circuit on the cadence gate (each still writes one `gate_reason="cadence_not_ready"` record per configured URL).

## Memory model demos

```bash
uv run python scripts/demo/private_memory.py       # a private fact: its owner finds it, user 2 does not
uv run python scripts/demo/shared_memory.py        # a shared note: user 2 reads user 1's note
uv run python scripts/demo/untrusted_content.py    # an injection planted in a shared note (live LLM)
```

The first two call `planazo.memory.facts` directly and need no API key. `untrusted_content.py` records what a real model does when a prompt injection reaches it through shared memory, so it needs a real `OPENCODE_API_KEY` and is not part of `uv run pytest` — the same opt-in-live convention as the gate tests above. With no key it prints one line and returns without calling the provider.

Each script redirects both store roots (`memory.facts.MEMORY_ROOT`, `storage.db.DB_PATH`) into a throwaway temp directory, so a demo run never writes to `var/`. Each writes its trace to `../docs/evidence/<name>.md`, gitignored — regenerate a trace by rerunning the script rather than committing it.

## Layout

```
.                                (repo root)
├── pyproject.toml
├── data/rules/            committed markdown rules; memory/rules.py re-reads them on every call
├── scripts/demo/          the three memory-model demos; traces land in docs/evidence/
├── src/planazo/           the domain package (one folder per bounded context)
│   ├── catalog/           Event + repository + save_event/search_events tools
│   ├── identity/          UserRecord + PreferenceRecord + repository
│   ├── approval/          ApprovalDecision + repository + ApprovalGate
│   ├── memory/            facts.py (private/shared JSON docstore), rules.py (markdown rules), api.py (user-bound memory tools)
│   ├── query/             interpreter.py (natural-language → SearchIntent, one Zen call, typed fallback)
│   ├── monitor/           out-of-band LLM-as-judge over run logs
│   ├── storage/           db.py (connection + schema_v1.sql only)
│   ├── config.py          shared env-check helper
│   ├── sources/           RawPost + MediaAsset + config; Instagram adapter (Dockerized); anon + hikerapi discovery clients
│   ├── scheduler/         run_tick + planazo-scheduler CLI; scan_state repository; SchedulerRunRecord audit log
│   └── agents/            loop.py (generic runtime), event_agent.py (composition root), cli.py
├── src/tools/
│   ├── schema.py           schema_for() — derives a tool's JSON schema from its signature/docstring
│   └── tools.py             the two calendar reference tools (kept until v0.2's real Google Calendar)
├── src/agentlib/
│   ├── core.py             call() / Result / cost() — the OpenCode Zen wrapper
│   └── tools.py             tool-calling entry point (re-exports core)
└── tests/
```

Layout is governed by [ADR 0008 — Domain-driven module layout](docs/adr/0008-domain-driven-module-layout.md) (bounded contexts) and [ADR 0009 — Repository root layout](docs/adr/0009-repo-root-layout.md) (why the outer `agent/` folder was retired).

See [`AGENTS.md`](AGENTS.md) for conventions and the full data-contract table, and [`docs/adr/`](docs/adr/) for why this stack and this tool/gate shape were chosen.
