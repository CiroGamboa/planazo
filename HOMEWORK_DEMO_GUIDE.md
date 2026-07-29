# Planazo demo guide

This is a short map from each course requirement to Planazo's implementation.
Use it during the presentation: start with the answer in the **What we show**
column, then open the linked implementation or test if the teacher asks how it
works. This document describes the project, not the individual submission
process.

## One-minute project explanation

Planazo is a Barcelona event-discovery assistant. It receives a request,
interprets it into a validated search intent, searches the validated event
catalog, filters and ranks results deterministically, and can prepare a
calendar action only after explicit approval. It is agentic because its
Recommender uses a hand-written **observe → reason → act → verify** loop, not a
fixed script or an agent framework.

The main safety idea is: **untrusted text is data, every boundary is typed, and
external actions require approval**. The architecture is documented in
[MVP architecture](docs/MVP-ARCHITECTURE.md) and the rules enforced across the
repository are in [AGENTS.md](AGENTS.md).

---

## tools, loop, approval, errors

| Requirement | What we show / say | Where it is implemented and tested |
| --- | --- | --- |
| Two real, team-designed tools | `search_events` reads validated events from SQLite; `save_memory` persists a user fact to JSONL; `save_event` persists extracted catalog events. Their schemas have action names, descriptions, typed parameters, and validation. | [catalog tools](src/planazo/catalog/tools.py), [memory tool closures](src/planazo/memory/api.py), [event schema](src/planazo/catalog/models.py), [tool-schema tests](tests/test_tools_schema.py) |
| Observe → reason → act → verify loop | `run_loop` calls the model, checks for tool calls, dispatches them, appends the tool result, and repeats. It stops on a model answer, truncation, or `max_steps`. | [hand-written loop](src/planazo/agents/loop.py), [loop tests](tests/test_agents_loop.py) |
| Explicit stop condition | The loop has a configurable `max_steps` cap; normal runs also stop when the model returns no tool calls. The curator has its own capped loop too. | [loop stopping logic](src/planazo/agents/loop.py), [curator max-step policy](src/planazo/curator/agent.py) |
| Human approval for irreversible action | Calendar creation is behind `ApprovalGate`. The terminal and Telegram surfaces ask the user; a declined action is not dispatched. Read-only search and reversible drafts are not gated. | [approval gate](src/planazo/approval/gate.py), [calendar tools](src/planazo/calendar/), [CLI gate](src/planazo/agents/cli.py), [gate tests](tests/test_agents_gate_live.py) |
| A tool error becomes a branch | A raised tool, unknown tool, or non-serializable result becomes `{"tool_failed": true, ...}` rather than a fake success. Catalog, extraction, memory, and Recommender failures use typed error states. | [failure marker](src/planazo/agents/loop.py), [typed catalog errors](src/planazo/catalog/tools.py), [loop tests](tests/test_agents_loop.py) |

**Good demo sentence:** “The model proposes a tool call, but our code owns the
tool schema, dispatch, error branch, stop condition, and approval decision.”

---

## memory, multiple agents, and monitoring

### 1. Three stores plus push and pull context

| Requirement | Planazo answer | Where to open |
| --- | --- | --- |
| Relational domain store | SQLite stores structured `Event`, user, preference, approval, run, and catalog state rows. Events are queryable/filterable records, not a key-value dump. | [catalog repository](src/planazo/catalog/repository.py), [storage migrations](src/planazo/storage/migrations/) |
| Non-relational memory store | Free-form facts and event notes live in JSONL document stores, separated into private-user and shared scopes. | [facts and notes](src/planazo/memory/facts.py), [memory models](src/planazo/memory/models.py) |
| Markdown operating rules | Markdown files in `data/rules/` are loaded on every Recommender run, so an operator can change rules without changing Python. | [rule loader](src/planazo/memory/rules.py), [rules tests](tests/test_memory_rules.py) |
| Push context | Rules, bounded validated preferences, and safe intent fields are assembled before the loop. Raw retrieved content never enters the system prompt. | [Recommender composition](src/planazo/agents/event_agent.py), [preference safety ADR](docs/adr/0011-preference-push-context-safety.md) |
| Pull context | The model must call `retrieve_memory` or `retrieve_notes` when it needs facts/notes. Results arrive as tool output, not system instructions. | [memory API](src/planazo/memory/api.py), [memory-resurface tests](tests/agents/test_memory_resurfaces.py) |

### 2. Facts versus rules

- A **fact** is a cued, free-form memory, for example a user preference. The
  model decides whether it is relevant after retrieval.
- A **rule** is operator-authored behavior guidance that is always pushed into
  the run; the model does not decide whether the rule exists.

Open [ADR 0004](docs/adr/0004-three-store-memory-model.md) for the rationale,
or run/read [memory resurface tests](tests/agents/test_memory_resurfaces.py).

### 3. Private, shared, and untrusted memory

| Question | Answer |
| --- | --- |
| How does private memory stay private? | Tool functions are closures bound to the authenticated `user_id`; the model never receives a `user_id` parameter it can change. Private files are scoped by that validated identity. |
| How is shared memory shared? | A shared scope has a common JSONL store, so another user can retrieve shared facts/notes. |
| Why is shared content safe? | It is returned only as a tool result. It is never concatenated into the system message, so a planted “ignore instructions” note is data, not an instruction. |

Show [ADR 0004](docs/adr/0004-three-store-memory-model.md),
[memory API](src/planazo/memory/api.py), and the scenario tests in
[tests/agents](tests/agents/). The evidence-trace names referenced by the
architecture are `private-memory.md`, `shared-memory.md`, and
`untrusted-content.md` under `docs/evidence/` when generated for a demo.

### 4. Executor plus another agent

Planazo has a real split of labor:

- The **Recommender** handles the user request, catalog search, filtering,
  preferences, and candidate result.
- The **Extractor** handles one source URL/post and returns a typed
  `ExtractionResult`; it has a written delegation brief, a limited tool set,
  and an effort/step budget.
- They coordinate through shared catalog state (`events`) and correlated run
  identifiers/audit records. Shared coordination is hard to debug because a
  later agent can observe a prior agent's data; run IDs and structured audit
  logs make the hand-off traceable.

Open [Extractor agent and delegation brief](src/planazo/agents/extractor.py),
[multi-agent ADR](docs/adr/0005-multi-agent-shape.md), and
[extractor tests](tests/test_agents_extractor.py).

### 5. Monitor on its own clock

The monitor is separate from the request loop. It reads completed run logs
after the fact and uses named categorical verdicts plus a required rationale;
it does not return a vague 1–10 score. Its job can report prompt-adherence or
task-completion problems with the expected-versus-observed reason.

| Show | File |
| --- | --- |
| Monitor model, categorical grades, rationale invariant | [monitor models](src/planazo/monitor/models.py) |
| Judge and scheduled service | [judge](src/planazo/monitor/judge.py), [service](src/planazo/monitor/service.py) |
| Run-log writer | [monitor logging](src/planazo/monitor/logging.py) |
| Tests | [monitor tests](tests/test_monitor_judge.py), [service tests](tests/test_monitor_service.py) |
| Design decision | [ADR 0007](docs/adr/0007-monitor-scheduling-and-grades.md) |

---

## real channel, triggers, silence, queue, admin helper

| Requirement | What we show / say | Where it is implemented and tested |
| --- | --- | --- |
| Real channel | Planazo has a Telegram bot surface. Incoming Telegram payloads are validated before they reach application code. | [bot app](src/planazo/bot/app.py), [surface](src/planazo/bot/surface.py), [bot tests](tests/test_bot_app.py) |
| Disposable identity | The bot uses a `TELEGRAM_BOT_TOKEN` from `.env`; the repository does not contain a token. For the demo we use a dedicated bot account, not a personal account. | [.env example](.env.example), [bot configuration](src/planazo/bot/config.py) |
| Non-message triggers | Scheduled ingestion checks configured Instagram sources on its cadence; the catalog curator runs separately on a daily cron. These background paths are independent of a Telegram message. | [scheduler CLI](src/planazo/scheduler/cli.py), [scheduler service](src/planazo/scheduler/service.py), [curator CLI](src/planazo/curator/cli.py), [ADR 0020](docs/adr/0020-catalog-curator-agent.md) |
| Silence branch with a record | A scheduler tick can decide that a source is not due, was already processed, or should be skipped after repeated failures. It deliberately performs no extraction/LLM call and writes a `SchedulerRunRecord` with a named `gate_reason`. | [scheduler models](src/planazo/scheduler/models.py), [scheduler audit](src/planazo/scheduler/audit.py), [acceptance evidence](docs/evidence/m3.5-scheduler-acceptance.md) |
| Queue while a turn is active | `PerUserQueue` serializes one sender's updates in FIFO order, bounds waiting messages, acknowledges queued work, and drops overflow explicitly. Different users can proceed concurrently. | [queue](src/planazo/bot/queue.py), [bot wiring](src/planazo/bot/app.py), [queue tests](tests/test_bot_queue.py), [ADR 0019](docs/adr/0019-per-user-message-serialization.md) |
| Admin subagent | The scheduled **Catalog Curator** is an admin-scoped agent with catalog-maintenance tools that normal Recommender/user paths do not have: archive stale events, merge duplicates, and correct categories. It has its own audit log and dry-run mode. | [curator agent](src/planazo/curator/agent.py), [curator tools](src/planazo/curator/tools.py), [ADR 0020](docs/adr/0020-catalog-curator-agent.md), [first-tick evidence](docs/evidence/m-curator-first-tick.md) |

**Queue trade-off to explain:** it is intentionally in-memory, so queued work
does not survive a process restart. That is acceptable for the current single
bot process; a persistent broker is a later scaling decision.

---

## Likely teacher follow-up questions

### “Where is the autonomy, and where are the limits?”

The Recommender chooses which read tools to call and can ask for clarification;
the Extractor chooses how to complete its delegated extraction; the Curator
can make catalog-maintenance decisions. Limits are enforced in code: Pydantic
schemas, bounded loops, typed error results, identity-bound closures, approval
gates, and audit records.

### “Why not put everything in one agent or one database?”

The split matches different responsibilities and data shapes. SQLite is for
structured, queryable events; JSONL is for flexible memory; Markdown is for
operator-editable rules. The Extractor isolates messy source parsing from the
user-facing Recommender, while the Curator isolates privileged catalog changes.
The trade-off is more hand-offs, so we use typed objects, run IDs, and logs.

### “How do you prove it works?”

Run the suite from the repository root:

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

For live/demo evidence, use the scheduler and curator evidence documents above.
The unit tests mock model/network boundaries; live tests are opt-in because
they require real credentials and can cost money.

### “What happens if the LLM or a source gives bad data?”

It is validated at the boundary. Invalid events, invalid preference rows,
invalid memory input, missing trusted search origin, tool exceptions, and
malformed tool envelopes become explicit typed outcomes. We do not silently
return an unfiltered search or a partial valid-looking event.

### “How do you protect against prompt injection?”

Retrieved captions, notes, and source content are never put into the system
prompt as instructions. They remain structured tool results. The trusted
system context contains only rules, bounded validated preferences, and safe
intent fields; geographic origin coordinates are intentionally redacted.

## Suggested demo order

1. Start the Telegram bot and show a normal safe request.
2. Open `run_loop` to show the hand-written loop and max-step stop.
3. Show an approval prompt for a calendar action and decline it once.
4. Show one memory fact/rule example and explain private/shared scope.
5. Show the Extractor delegation brief and a typed extraction result.
6. Show a scheduler record with a `gate_reason` silence branch and the
   curator dry-run evidence.
7. Finish with `uv run pytest` and the relevant test file for any question.

For deeper design rationale, use the numbered ADRs in [docs/adr](docs/adr/).
