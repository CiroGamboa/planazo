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

`planazo.query.interpret(text)` translates a free-text `/find` message into a validated `planazo.schemas.events.SearchIntent` via a single Zen `call()` on the CHEAP model with one function-call tool (`_record_search_intent`, whose signature `schema_for` reflects into the tool schema). It is **not a registered tool** — the Recommender's loop never sees it; the bot's `/find` handler (M6) will be its only caller. On any failure — the LLM raises, the reply carries no tool call, the tool name does not match, or Pydantic rejects the wire arguments — `interpret` returns a degraded `SearchIntent` (today -> today+72h, `city="Barcelona"`, empty `categories`) tagged `error_type="interpreter_fallback"` instead of raising or silently defaulting. Callers **must** branch on `error_type` before reading any other field; the two shapes are structurally identical apart from that tag.

### The calendar reference tools

These two are the calendar reference implementation, reachable only via `--calendar` (or `run_once(calendar_enabled=True)`). They are JSON-backed and stay untouched until v0.2's real Google Calendar wiring replaces them; `confirm_and_create_calendar_event` is the only irreversible tool in the tree, so it is also the approval gate's only end-to-end demonstration.

- **`save_event_candidate`** (reversible) — persists one normalized event candidate to `var/event_candidates.json`. Runs without approval; re-saving a correction is just another write. Validates input through `planazo.schemas.events.EventCandidateInput` and returns a typed `error_type` (`invalid_event_data`, `low_confidence_extraction`) instead of persisting bad or unreliable data.
- **`confirm_and_create_calendar_event`** (irreversible) — looks up a previously saved candidate by `event_id` and creates the calendar entry in `var/calendar_events.json`, optionally emailing invitees. This is visible to a third party, so the CLI gates it behind a terminal `y/N` approval prompt before dispatch; declining feeds the model a `DECLINED_RESULT` marker instead of running the tool. Returns a typed `error_type` (`invalid_confirmation_data`, `missing_invitees`, `event_not_found`) on bad input instead of silently creating a broken entry.

## The loop

`planazo.agents.loop.run_loop` drives `agentlib.tools.call` across turns, dispatching whatever tool calls the model requests and feeding results back, until either:

- the model answers with no further tool calls (`stopped="answered"`, or `"truncated"` if the output cap cut it off) — the done signal, or
- `max_steps` is reached without an answer (`stopped="max_steps"`, `answer=None`) — the explicit backstop.

It is completely generic over `tools`/`registry`, so it has no Planazo-specific code in it; `planazo.agents.event_agent.run_once` is the one place the event-discovery tool set is bound to it.

`run_loop`'s optional `system` argument is prepended once as the run's system message, ahead of the user's. `run_once` is what assembles it — the markdown rules from `data/rules/`, re-read on every call, plus the bound user's preference rows — so that is the run's whole push context. Everything else the model sees, tool results included, arrives as a `function_call_output`, never in the system role.

### Live tests

```bash
uv run pytest -m live tests/test_agents_gate_live.py -v -s   # hits the real LLM; needs a real OPENCODE_API_KEY
```

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
agent/
├── pyproject.toml
├── data/rules/            committed markdown rules; memory/rules.py re-reads them on every call
├── scripts/demo/          the three memory model demos; traces land in ../docs/evidence/
├── src/planazo/
│   ├── schemas/           Pydantic v2 boundary models (events.py, domain.py, memory.py)
│   ├── storage/           db.py (connection + schema_v1.sql), dao.py (the SQLite DAO)
│   ├── memory/            facts.py (private/shared JSON docstore), rules.py (markdown rules), api.py (the user-bound memory tools)
│   ├── query/             interpreter.py (natural-language -> SearchIntent, one Zen call, typed fallback)
│   └── agents/            loop.py (generic), event_agent.py (tool binding), cli.py
├── src/tools/
│   ├── schema.py           schema_for() — derives a tool's JSON schema from its signature/docstring
│   └── tools.py             the two calendar reference tools
├── src/agentlib/
│   ├── core.py             call() / Result / cost() — the OpenCode Zen wrapper
│   └── tools.py             tool-calling entry point (re-exports core)
└── tests/
```

See [`../AGENTS.md`](../AGENTS.md) for conventions and the full data-contract table, and [`../docs/adr/`](../docs/adr/) for why this stack and this tool/gate shape were chosen.
