# ADR 0021 — Shrink the Recommender's tool set: retract `save_preference` and `dispatch_extraction`

**Status:** Accepted
**Date:** 2026-07-29
**Supersedes:** the `save_preference`-half of [ADR 0013](0013-recommender-mutation-and-clarification-boundaries.md). ADR 0013's `ask_user`-half stays in force.
**Related:** [ADR 0004](0004-three-store-memory-model.md) (memory tools remain), [ADR 0005](0005-multi-agent-shape.md) (dispatch_extraction remains reachable from the Extractor and the scheduler, just not from the Recommender), [ADR 0013](0013-recommender-mutation-and-clarification-boundaries.md) (partially superseded), issue #109.

## Context

`answers.txt` message 3 asked the bot for music events. Between the user's ask and the Recommender's reply, the LLM silently called `save_preference("category", "music")`. From message 4 onward, `_preferences_text` pushed `- 'category': 'music'` into every Recommender system message. A fresh greeting on message 4 came back as "0 events for the Music filter." The user never asked to save that preference; the LLM saved it as a side effect of answering one search query.

Both `save_preference` and `dispatch_extraction` were registered as Recommender-loop tools under ADR 0013 (§ Decisions D1 and D2). The reasoning at the time — "the Recommender may need to persist a preference the user just stated, or hand off extraction of a URL the user pasted" — assumed the LLM would only fire these writers on explicit user asks. In practice, LLM tool-choice under a rich schema drifts toward writing more, not less.

`dispatch_extraction` has not been observed misfiring, but it belongs to the same class of authority: a read-only recommendation turn should not trigger side-effectful extraction. Both writers exist for legitimate callers elsewhere — this ADR unregisters them from the *Recommender's* loop only.

## Decision

### D1: Retract `save_preference` from the Recommender's tool registry

The Recommender no longer registers `save_preference`. The closure declaration, its `schema_for(...)` entry, and its registry line are all removed from `planazo/agents/event_agent.py::run_once`. The `_preferences_text` push block stays — reading preferences is unaffected; only *writing* them from inside the Recommender is retracted.

**Where `save_preference` still lives:** the `/prefs set` bot command (via `identity/repository.py::set_preference`) and the clarification-answer path in `conversation/service.py`. Both continue to write preferences on explicit user intent (a slash command, or answering a clarification question). No changes to `PreferenceRecord` or the `preferences` schema.

### D2: Retract `dispatch_extraction` from the Recommender's tool registry

Same shape as D1: the `build_dispatch_extraction(user_id)` lazy-import and its schema/registry lines are removed from `run_once`. The shared builder (`extraction.tools::build_dispatch_extraction`) survives untouched — the Extractor and the scheduler both use it on their own composition roots.

### D3: Keep the four memory tools in the Recommender's registry

`build_memory_tools(user_id)` continues to return the four memory tools (`retrieve_memory`, `save_memory`, `retrieve_notes`, `save_note`) that ADR 0004's three-store model authorized. The Recommender needs them to satisfy the demo requirements in ADR 0004 §"Scenario 1..3" — a saved fact resurfaces on its cue; the LLM acts on it without being told; private stays private; shared reaches everyone; shared content is untrusted. Removing them would idle the module without fixing any observed bug — the drift ADR 0021 aims to avoid.

### D4: One paragraph of memory-writer prompt discipline

A new sibling markdown file `data/rules/010-memory-writer-discipline.md` names when `save_memory` and `save_note` may fire:

- The user explicitly asks the agent to remember something.
- The same preference has been implied across at least two turns of the current conversation.

Never as a side effect of answering a single search query. Never for a fact the user has not actually said. The file lands under `memory/rules.py::load_rules`'s alphabetized load order, so its content ships in every Recommender system message.

## Consequences

- `answers.txt` message 3's persistence-bug root cause disappears: with `save_preference` unregistered, no LLM tool call can silently write to the identity aggregate mid-recommendation. Message 4's "0 events for the Music filter" branch is unreachable from the Recommender's side.
- Test surface shrinks: any test that asserted the Recommender writing preferences during a recommend turn is dead code (AGENTS.md Rule 8/9) and is deleted, not updated.
- Tool-schema surface shrinks by two tools per Recommender turn. Lower LLM cost per turn (fewer schemas fed as tool definitions), lower drift potential per turn.
- ADR 0013 is partially superseded — its `save_preference` decision retracts here; its `ask_user` decision stays.

## Rejected alternatives

1. **Guard `save_preference` with a heuristic instead of unregistering.** *Rejected.* The failure mode is LLM tool-choice, not a broken write. A guard is another surface to reason about; removing the tool is the safer null hypothesis. The tool remains available on its legitimate callers.
2. **Also unregister the four memory tools.** *Rejected — deliberately.* ADR 0004's demo requires them; removing them idles the memory module without fixing any observed bug. The follow-up ticket (#113 in M6) teaches the LLM to actively use them; this ADR is about pruning misuse, not about killing use.
3. **Delete `save_preference` from `identity/` entirely.** *Rejected.* The bug is Recommender-side authority, not the primitive. `/prefs set` and clarification answers depend on the primitive; they are the correct write surfaces.
4. **Add an approval gate on `save_preference`.** *Rejected.* Approval gates exist for irreversible cross-system side effects (calendar, broadcast). A preference row is neither irreversible nor cross-system; adding a gate here overpays for the problem.

## Follow-up work (deferred)

- **Ticket #113 (M6)** — teach the LLM to actively use `save_memory` / `retrieve_memory` / `save_note` / `retrieve_notes` so ADR 0004's demo runs end-to-end through the Recommender. This ADR only proves the wiring survives the shrink.
- **Ticket #112 (M6)** — route greetings and meta-questions upstream of the Recommender so a "Hi" doesn't spend a Recommender loop at all (ADR 0020).
