# Planazo Agent

The hand-rolled observe -> reason -> act -> verify agent that powers Planazo's event discovery: it calls source/extraction tools, persists normalized event candidates, ranks them, and — only after explicit chat approval — creates the user's calendar entry.

No agent framework, no API server, no frontend here (see `AGENTS.md` rule 5 and `docs/adr/`) — just the loop, the tool registry, and the guardrails, all plain Python.

## Quick start

```bash
uv sync                                                                  # install
uv run planazo-agent --calendar "save a tech event evt-1 called AI Meetup at 2026-08-01T19:00:00 in Barcelona, confidence 0.9"  # one-shot
uv run planazo-agent                                                     # interactive REPL
```

`agentlib` (the LLM wrapper) needs `OPENCODE_API_KEY` set in a `.env` file at the repo root; copy `../.env.example`. If the key is unset, the CLI prints one actionable line and exits without calling the provider; if the key is present but invalid or the provider errors, it prints a single-line message — never a traceback.

In the REPL, type a prompt at the `agent> ` indicator; `exit`/`quit`, EOF (Ctrl-D), or Ctrl-C ends the session. Each line runs independently — no history carries across turns.

Options:

- `--strong` and `--model {cheap,strong}` select the model role and are mutually exclusive; passing both is a usage error. The default is the cheap role.
- `--max-steps N` caps the loop's steps (`N` must be >= 1).
- `--calendar` adds the two calendar reference tools to the run's tool set. Without it the model is offered only `search_events`.

Output shape: a per-step tool trace (`step N: tool(args) -> result`), then a separated final block with the answer (or a `(no final answer — hit max steps)` notice), the step count, and the stop reason.

## Commands

```bash
uv run pytest              # tests (the LLM is mocked; live tests are opt-in, see below)
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy src            # types
```

## Tools

`search_events` is the default — and, without `--calendar`, the only — tool. It queries the SQLite domain store at `var/planazo.db` for stored events, filtered by `category`, `city`, and `start_after` (an empty string means "no filter on that field"), and returns `{"events": [...], "total": N}` or a typed `invalid_search_filter` error. Its writing counterpart, `save_event`, lives beside it in `planazo.storage.dao` for the extraction path; the domain store's shape and its two dao tiers are [ADR 0003](../docs/adr/0003-sqlite-domain-store.md).

Every tool returns a typed `error_type` on bad input rather than persisting something partial. A tool that raises anyway (bug, disk error) is caught one layer up, inside `planazo.agents.loop.run_loop`'s dispatch, and fed back to the model as a `tool_failed` marker rather than crashing the run or looking like valid data.

### The calendar reference tools

These two are the calendar reference implementation, reachable only via `--calendar` (or `run_once(calendar_enabled=True)`). They are JSON-backed and stay untouched until v0.2's real Google Calendar wiring replaces them; `confirm_and_create_calendar_event` is the only irreversible tool in the tree, so it is also the approval gate's only end-to-end demonstration.

- **`save_event_candidate`** (reversible) — persists one normalized event candidate to `var/event_candidates.json`. Runs without approval; re-saving a correction is just another write. Validates input through `planazo.schemas.events.EventCandidateInput` and returns a typed `error_type` (`invalid_event_data`, `low_confidence_extraction`) instead of persisting bad or unreliable data.
- **`confirm_and_create_calendar_event`** (irreversible) — looks up a previously saved candidate by `event_id` and creates the calendar entry in `var/calendar_events.json`, optionally emailing invitees. This is visible to a third party, so the CLI gates it behind a terminal `y/N` approval prompt before dispatch; declining feeds the model a `DECLINED_RESULT` marker instead of running the tool. Returns a typed `error_type` (`invalid_confirmation_data`, `missing_invitees`, `event_not_found`) on bad input instead of silently creating a broken entry.

## The loop

`planazo.agents.loop.run_loop` drives `agentlib.tools.call` across turns, dispatching whatever tool calls the model requests and feeding results back, until either:

- the model answers with no further tool calls (`stopped="answered"`, or `"truncated"` if the output cap cut it off) — the done signal, or
- `max_steps` is reached without an answer (`stopped="max_steps"`, `answer=None`) — the explicit backstop.

It is completely generic over `tools`/`registry`, so it has no Planazo-specific code in it; `planazo.agents.event_agent.run_once` is the one place the event-discovery tool set is bound to it.

### Live tests

```bash
uv run pytest -m live tests/test_agents_gate_live.py -v -s   # hits the real LLM; needs a real OPENCODE_API_KEY
```

## Layout

```
agent/
├── pyproject.toml
├── src/planazo/
│   ├── schemas/           Pydantic v2 boundary models (events.py, domain.py)
│   ├── storage/           db.py (connection + schema_v1.sql), dao.py (the SQLite DAO)
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
