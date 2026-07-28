# 0011 — Telegram bot interface abstraction

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Supersedes:** [`0010-extensibility-interfaces.md`](0010-extensibility-interfaces.md)'s **`UserSurface` declaration only**. That ADR's four-seam strategy, its structural-typing discipline, and its `EventSource` / `Repository[T]` / `AgentLoop` declarations all remain in force; only the three-member `UserSurface` shape it shipped as a stub is retired.
- **Superseded by:** [`0014-per-user-message-serialization.md`](0014-per-user-message-serialization.md)'s **`concurrent_updates` default and the "never crosses threads" consequence it implied, only** — see the two notes inline below (Decision → "Long polling, one connection per command"; Consequences → Negative → "Blocking work on the event-loop thread stalls every user"). Everything else here — the `UserSurface` shape, the PTB-free module boundary, session mapping, and the approval-gate threading contract off the event loop — remains accurate; `build_application` still runs the synchronous agent loop off-thread via `asyncio.to_thread`, exactly as decided below.
- **Relates to:** [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (bounded contexts — `planazo.bot/` is the surface seam), [`0002-event-tool-contracts-and-approval-gate.md`](0002-event-tool-contracts-and-approval-gate.md) (the approval gate this ADR gives a threading contract), [`../MVP-ARCHITECTURE.md`](../MVP-ARCHITECTURE.md#1-telegram-bot--srcplanazobot).

## Context

M5 (#21) lands the first user-facing surface. Before it, `main` had an identity aggregate, a memory API, a domain store, and two agent loops, but nothing a human could talk to: `src/planazo/bot/` did not exist, and `grep -rn "UserSurface" src/` matched only the Protocol's own declaration. Four forces shaped what follows.

**1. `UserSurface` as ADR 0010 declared it has no possible implementation.** ADR 0010 shipped four Protocols explicitly "empty of consumers" — stubs written from the shape of the then-current code. M5 is the first ticket to actually implement one, and two of `UserSurface`'s three members turn out to describe a runtime that does not exist:

- `read_message() -> str` is a blocking *pull*. Nothing in the runtime pulls. `planazo.agents.event_agent.run_once(prompt, ...)` takes the user message as an *argument*, and the terminal surface (`agents/cli.py::_repl`) owns intake itself via `input()`. `python-telegram-bot` (PTB) is the opposite model: an `Application` long-polls the Bot API and *pushes* each update into an `async def` handler. There is no point in a PTB program at which `read_message()` could be called, and no caller that would call it.
- `approval_callback() -> Callable[...]` is unimplemented by the very surface ADR 0010's docstring claims conforms. `agents/cli.py:155` builds `ApprovalGate(tool_names=..., approve=_terminal_approve)` from a bare module-level function; there is no surface object holding it. Approval already has its own Protocol in the same module — `ApprovalCallback` — declaring exactly the shape `planazo.approval.ApprovalGate.approve` holds and `run_loop` calls. The `UserSurface` member is a second, unused spelling of the same seam.

Only `reply` survives contact with a real surface, and it has to be `async` for one: PTB dispatches into coroutines and `Bot.send_message` is awaitable.

**2. PTB publishes no downstream test harness.** Probed against PTB 22.8: `telegram.testing` does not exist, and the internal `PytestBot` / `make_bot` helpers PTB uses for its own suite are not exported. Both obvious entry points into a built `Application` are closed offline — `Application.process_update()` raises ``RuntimeError: This Application was not initialized via `Application.initialize`!`` unless `initialize()` runs, and `initialize()` calls `getMe` over the network (``telegram.error.InvalidToken: The token `123:ABC` was rejected by the server.``). #21's acceptance criterion was renegotiated on this evidence and now names four offline tiers instead; the Decision below records them as the accepted testing contract, not as a proposal.

**3. The approval seam is about to acquire a caller, and its hard constraint is threading, not signature.** #22 builds the Telegram inline-keyboard approval callback against `ApprovalGate`. The gate's `approve(tool_name, arguments) -> bool` is synchronous and blocking by design (ADR 0002), while the tap that answers it arrives as an asynchronous `CallbackQuery` update. Which thread the loop runs on decides whether that bridge works at all — see the Decision. This is the part that had to be written down before #22 starts, because getting it wrong produces a gate that *looks* like a working rule-3 guardrail while declining everything.

**4. Number 0011 was reserved for something else.** `docs/MVP-ARCHITECTURE.md`'s ADR table and the follow-up sections of ADRs 0005 and 0006 pre-reserved **ADR 0011 for the conditional Meetup / Eventbrite event-sources decision**. That decision has not been written and is conditional on either source shipping past POC; this ADR takes 0011 because it is the one that actually landed, and **the conditional Meetup / Eventbrite ADR is now 0012**. `docs/MVP-ARCHITECTURE.md` is mutable and has been renumbered. Three references cannot be: `docs/adr/0005-multi-agent-shape.md:155`, `docs/adr/0006-instagram-extraction-approach.md:76`, and `docs/adr/0006-instagram-extraction-approach.md:106` each read "ADR 0011, conditional", and `AGENTS.md:18` makes accepted ADRs immutable historical records. **Wherever ADRs 0005 and 0006 say "ADR 0011, conditional" for Meetup or Eventbrite, they mean ADR 0012.** This paragraph is that redirect; it exists because an ADR is the one document `AGENTS.md` rule 10 permits to carry history, and a dangling cross-reference is exactly what that exemption is for. ADR 0009 line 65 names the same cost for its own supersede of ADR 0001.

**Alternatives considered.**

- **Keep `UserSurface` as declared and write a Telegram adapter that satisfies it.** Rejected. `read_message()` on a push-model surface can only be implemented as a lie — an internal queue nothing feeds, or a `NotImplementedError`. A Protocol with a member every conformer must fake is worse than no Protocol.
- **Deprecate the two members rather than delete them.** Rejected on `AGENTS.md` rule 9. Both are provably unreferenced (`grep -rn "read_message\|approval_callback" src/ tests/` matches only the declarations), so a deprecation shim would be dead code with a scheduled cleanup nobody needs.
- **Webhooks instead of long polling.** Rejected for the MVP: a webhook needs a public HTTPS endpoint, a certificate, and a deployment target none of which exist, and it buys latency the bot does not need. Long polling runs from a laptop and a `.env`. Revisit when there is a hosted deployment.
- **Markdown or HTML `parse_mode` on replies.** Rejected. `/me` and `/prefs` echo user-supplied preference values back to the user; a value containing `*`, `_`, or `<` would be reinterpreted as formatting or make the send fail outright. Plain text costs nothing here and removes an entire escaping surface.
- **Let the bot import `planazo.agents` for a natural-language fallback now.** Rejected as out of scope and as the thing the invariant exists to prevent at this stage. #57 adds the free-text path deliberately, under its own review.

## Decision

Ship the Telegram bot as a **PTB-free command core behind a thin transport shell**, reshape `UserSurface` into the one member a push-model surface can honour, and bind the approval seam's threading contract.

**No LLM call originates in `bot/`, enforced by a source-text scan.** The bot layer is deliberately dumb — the four commands are CRUD against SQLite and nothing else. The guard is a static test that reads every `*.py` under `src/planazo/bot/` and asserts none contains the substring `agentlib`, mirroring #21's `grep -r 'agentlib' src/planazo/bot/`, and additionally asserts the scanned file list is non-empty so a package rename cannot make it pass vacuously. It is deliberately a **shallow text scan, not a transitive import walk** like `tests/test_trust_boundary.py`: the invariant is "no LLM call *originates* in `bot/`", not "`bot/` reaches no LLM code". A transitive walk would pass today and fail #57, which legitimately hands a user's message to `planazo.agents.event_agent` — whose import graph reaches `agentlib` — and that ticket is the intended evolution, not a violation.

**Five modules, one dependency direction.** `planazo.bot` splits so that only its edge knows about Telegram:

| Module | Holds | Imports `telegram`? |
| --- | --- | --- |
| `bot/app.py` | `Application` construction, one `CommandHandler` per command, the `Update` → `IncomingMessage` adapter, `main()` | yes |
| `bot/surface.py` | `TelegramSurface` — the reply channel, bound to a `Bot` + `chat_id` | yes |
| `bot/models.py` | `IncomingMessage` — the validated projection of one update | no |
| `bot/session.py` | `resolve_user` — Telegram id → internal `users` row | no |
| `bot/commands.py` | the four command coroutines and every user-facing literal | no |

Every command has the same PTB-free signature:

```python
async def handle_<cmd>(surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage) -> None
```

That signature is the load-bearing part of the layering. It makes the behaviour testable offline against real SQLite and a recording surface with no PTB object in the path; it lets #57 hand the same `UserSurface` to the agent loop without touching command code; and it puts every literal in one module for #55 to lift into `data/bot.yaml`.

**`IncomingMessage` is the trust boundary.** A Telegram update is an external payload, so it crosses into the system through a frozen Pydantic v2 model with `extra="forbid"` (`AGENTS.md` rule 1) carrying `telegram_user_id`, `display_name`, `telegram_handle`, and `text`. It imports nothing from `telegram`: the conversion from a PTB `Update` lives in `app.py`, which is what keeps the command layer transport-neutral. It is named for what it is — a validated *projection* of an update, not the update — because the same model is what a WhatsApp or web surface would produce.

**Session mapping is create-on-first-contact, keyed by `telegram_user_id`.** `resolve_user` delegates to `planazo.identity.get_or_create_user`, so the first message from an unseen Telegram id inserts exactly one `users` row and every later message reuses it. Get-or-create, not upsert: an existing row's `display_name` wins on a later contact. Broader registration is #56.

**`UserSurface` becomes one member.**

```python
class UserSurface(Protocol):
    async def reply(self, text: str) -> None: ...
```

`read_message()` and `approval_callback()` are deleted outright, on the evidence in Context §1. `ApprovalCallback`, declared in the same module, is unchanged and is *the* approval seam — its docstring is rewritten to say so directly rather than describing itself as a `UserSurface` member. Replies are **plain text with no `parse_mode`**; that is a contract, not a default.

**Testing contract — four offline tiers.** This replaces #21's original "python-telegram-bot test harness" wording, which Context §2 shows cannot be met. The four tiers together cover strictly more than a harness wrapper would, and none of them mocks anything the issue asked to be real:

- *behaviour* — command coroutines against real SQLite and a recording `UserSurface`, no PTB object in the path;
- *routing* — `CommandHandler.check_update()` driven by genuine `telegram.Update` objects carrying a `BOT_COMMAND` `MessageEntity`, proving each of the four commands actually dispatches, including the `/cmd@botname` form;
- *registration* — the built `Application`'s handler set;
- *adaptation* — `Update` → `IncomingMessage` conversion against real `telegram.User` / `Chat` / `Message` instances.

The live `uv run python -m planazo.bot` + `/start` check stays a manual PR test-plan item, because it needs a real bot token.

**Threading contract for the approval seam.** This is binding on #22 and on every later handler that runs the agent loop:

- Any handler that invokes the **synchronous** agent loop must run it **off the event-loop thread**, via `asyncio.to_thread`.
- The off-thread callee opens its **own** connection with `planazo.storage.db.connect()`; the adapter's connection never crosses the thread boundary. `sqlite3` connects with `check_same_thread=True`, so a connection opened on the event-loop thread and used inside `asyncio.to_thread` raises `sqlite3.ProgrammingError`.
- The synchronous `ApprovalCallback` bridges back to the loop's coroutines with `asyncio.run_coroutine_threadsafe(...)`, blocking on the returned `concurrent.futures.Future`.

The failure mode this prevents is not a slowdown, it is a silent total failure. If the agent loop instead ran *on* the event-loop thread, blocking inside the approval callback would stop the event loop from dispatching the `CallbackQuery` update that carries the user's tap — the tap can never arrive, so the gate times out, and rule 3's guardrail degrades into a **permanent decline that looks like it is working**. PTB's `Application.concurrent_updates` defaults to `1` (verified on 22.8), so this is total rather than intermittent: nothing else gets dispatched either. Deleting `approval_callback()` from `UserSurface` does not corner #22 — `ApprovalCallback` survives with exactly the signature it needs. The threading is the part that had to be written down.

**Long polling, one connection per command.** `Application.run_polling()`, no webhook. Each command invocation opens and closes its own SQLite connection in the adapter; with `concurrent_updates == 1` updates are processed one at a time, so a per-invocation synchronous connection never crosses threads. *(Superseded by [ADR 0014](0014-per-user-message-serialization.md): `build_application` now sets `concurrent_updates=True`, so updates from different senders run concurrently. A per-invocation connection never crossing threads is now a property of `planazo.bot.queue.PerUserQueue` serializing each sender's own dispatches, not of `concurrent_updates == 1`.)*

## Consequences

### Positive

- **The Protocol now describes something that exists.** `UserSurface` has a real conformer, checked where it is constructed by `uv run mypy src`, instead of three members and zero implementations.
- **The command layer never learns what Telegram is.** `commands.py`, `session.py`, and `models.py` import no transport. A WhatsApp or web surface reuses all three by supplying its own `reply` and its own `Update` → `IncomingMessage` adapter.
- **The tests are fully offline and hit real SQLite.** No network, no `Application.initialize()`, no mocked database — the four tiers exercise genuine PTB objects for routing and adaptation and genuine SQLite for behaviour.
- **#22 inherits a written contract instead of a discovery.** The off-thread requirement and the `run_coroutine_threadsafe` bridge are decided here, where they can be reviewed, rather than found by debugging a gate that declines everything.
- **The 0011 / 0012 renumbering has a single authoritative answer** that survives the immutability of ADRs 0005 and 0006.

### Negative / accepted trade-offs

- **`UserSurface` ships with exactly one conformer.** A Protocol defined by its only implementation is at risk of being shaped by it. The reshape is purely subtractive, which limits the damage, and #60 is the mitigation.
- **`async def reply` imposes a cost on the terminal CLI.** Making the synchronous REPL conform (#60) means either an `asyncio.run` per reply or an async REPL. Accepted: Telegram, WhatsApp, and web surfaces are all async, the terminal is the outlier, and forcing every future surface into a synchronous signature to spare it is the wrong trade.
- **Blocking work on the event-loop thread stalls every user.** With `concurrent_updates == 1` there is no concurrency to absorb it. Millisecond CRUD makes this a non-issue for the four commands, but it is precisely why the threading contract above is binding rather than advisory once #57 puts a multi-second loop on the same path. *(Superseded by [ADR 0014](0014-per-user-message-serialization.md): `concurrent_updates=True` now gives different senders' updates real concurrency, so blocking work on the event-loop thread stalls only the sender it belongs to, not every user. The threading contract itself — running the synchronous agent loop off-thread via `asyncio.to_thread` — is unchanged and remains binding.)*
- **Three "ADR 0011, conditional" references stay stale on disk** inside ADRs 0005 and 0006. This is the standing cost of the supersede-via-new-ADR pattern; the Context paragraph above is the redirect.
- **Plain-text replies mean no formatting, ever.** No bold command names, no code-formatted keys. Accepted in exchange for never escaping user-supplied preference values.
- **The bot layer cannot answer anything it does not have a command for.** Free text goes unanswered until #57. Deliberate: the invariant is the point of this milestone.

### Follow-ups

- **#22 — the approval inline keyboard** (`bot/approve.py`), built against `ApprovalCallback` and the threading contract above. Same milestone; no approval code ships here.
- **#60 — give the terminal CLI a `UserSurface` implementation**, so the Protocol ends up with two structurally different conformers rather than being defined by Telegram alone.
- **#61 — move the `ApprovalGate` Protocol declaration** out of `agents/loop.py` into `interfaces/surface.py`, the cleanup ADR 0010's own follow-ups named. Best sequenced after #22, so both approval implementations are visible.
- **#57 — free text routed to the agent loop.** It is the first caller bound by the off-thread requirement, and the first legitimate reason `bot/` reaches LLM code — which is why the invariant test is a source-text scan.
- **#55 — externalize the user-facing copy** into `data/bot.yaml`. Every literal introduced by the bot lives in `commands.py` and is that ticket's input.
- **ADR 0012 — Meetup / Eventbrite event sources**, conditional on either shipping past POC. Renumbered from the 0011 slot this ADR takes.
