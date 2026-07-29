# ADR 0020 — Router boundary and two-tool interpreter

**Status:** Accepted
**Date:** 2026-07-29
**Related:** [ADR 0013](0013-recommender-mutation-and-clarification-boundaries.md) (clarification boundary), [ADR 0016](0016-multi-turn-recommender-conversation.md) (multi-turn conversation), [ADR 0021](0021-recommender-tool-boundary-shrink.md) (Recommender tool set — same M6), issue #112.

## Context

`answers.txt` messages 1 and 2 — "Hi" and "Hola" — surfaced 10 unrelated events. Meta-questions like "what can you do?" got the same treatment: parsed as a search, dispatched to `run_once`, and answered with events instead of an explanation.

The root cause was interpreter scope. `query/interpreter.py::interpret` treated every user message as a search query. Its system prompt was "Read the user's free-text message describing what events they want to find, then call the `_record_search_intent` tool exactly once" and its contract was "Never answer in prose; always call the tool." A greeting therefore became a `SearchIntent` with the fallback 30-day window, which was dispatched to a full Recommender loop, which listed arbitrary events.

Two of the seven maintainer-named improvements in the follow-up brief map here:

1. "Conversation must feel natural — the intent is to lead the user to recommend events."
2. "The user may ask questions — we need to be able to route those questions."

Fixing the display layer alone (widening the fallback, adding a source-link preface) is not sufficient. A greeting must not spend a Recommender loop at all — no `agent_runs`, no `recommendations`, no `llm_decisions` row for that turn.

## Decision

### D1: `interpret(text) -> RoutedMessage` discriminated union

`planazo.query.interpret` now returns a Pydantic-v2 discriminated union `RoutedMessage`:

- `SearchRoute(kind="search", intent: SearchIntent)` — the parsed search query.
- `ChatRoute(kind="chat", answer: str)` — the LLM's own concise reply (1..500 chars) to a greeting, thanks, or meta-question.

Callers dispatch on `.kind`. The union is declared with `Field(discriminator="kind")` so a JSON payload roundtrips into the right variant without a manual `isinstance` check.

### D2: Two tools on the interpreter — `_record_search_intent` and `_reply_chat`

The interpreter's Zen `call()` registers BOTH tools. The system prompt guides the model on which one to call:

- Search intent → `_record_search_intent(...)` (existing).
- Greeting / small-talk / meta-question → `_reply_chat(text: str)` (new).

The prompt names both tools and gives explicit disambiguation guidance ("If the message is ambiguous (a bare category name like 'music' that could be a greeting-alternative or a search), prefer `_record_search_intent` — the Recommender's clarification loop is the right place to disambiguate on the search side").

### D3: The interpreter's fallback NEVER lands as `ChatRoute`

On any interpreter failure — LLM raises, reply carries no tool call, wrong tool name, Pydantic rejects the wire arguments, chat reply exceeds the 500-char cap — `interpret` returns a `SearchRoute` whose intent has `error_type="interpreter_fallback"` set. A failure that silently masqueraded as a chat reply would hide the uncertainty the display layer signals.

`_fallback_search_route()` is the shared helper; it lives on the `SearchRoute` branch by design.

### D4: `handle_user_message` dispatches on `RoutedMessage`

In `conversation/service.py::_handle_fresh_query`:

- `SearchRoute` — continue to `run_once` as today (unchanged).
- `ChatRoute` — return `ConversationReply(kind="chat", answer=routed.answer)` without opening a Recommender loop. The scratchpad's `updated_at` refreshes so operator queries see the turn; every other field is preserved (a `chat` turn does not invalidate a prior `last_recommendation_run_id` or `pending_clarification`).

A `chat` turn writes zero `agent_runs` / `recommendations` / `llm_decisions` rows.

### D5: Follow-up branches (detail lookup, more-results, clarification-answer) win over the router

The precedence in `handle_user_message` is unchanged. The router only applies on the fresh-query branch, which fires last. Explicit invariants:

- **A numeric "2" after a batch keeps meaning "tell me about #2"** — the detail-lookup branch fires first and consumes the message.
- **A sender mid-clarification whose next message is a greeting lands in the clarification-answer path** — the clarification-answer branch calls `interpret_search_only(text)` which returns a `SearchIntent` unconditionally (wrapping `interpret` and falling back if the router chose chat). This is codified in a new helper `query.interpreter.interpret_search_only` so the "more specific state wins" invariant does not depend on the LLM's classification.
- **The pattern-based follow-ups (`more results`, "tell me about #N`) already sit above the fresh path** and continue to do so — the router never sees a message that a follow-up branch consumes.

### D6: A new `chat_reply` message ID in `data/bot.yaml`

`bot/commands.py::_format_reply` gains a branch for `reply.kind == "chat"` that resolves `chat_reply` from the bot's message catalog. English and Spanish variants both pass the LLM's reply through as `{answer}` — the router already produced the message in the user's language.

## Consequences

- `answers.txt` messages 1, 2, and any future greeting/small-talk turn no longer spend a Recommender loop. The tick pays a single CHEAP-tier LLM call for the router turn itself and returns immediately.
- Latency for a greeting drops from ~4 LLM turns (interpret + run_once with tools) to 1 (router only).
- Meta-questions get a concise operator-facing description of `/find`'s capability from the router LLM directly. Correct-by-construction — the LLM cannot list nonexistent events because it never calls `search_events`.
- The clarification-answer path is now formally protected against router drift via `interpret_search_only`.
- The bot's response taxonomy grows by one `kind` ("chat"). Every surface that renders `ConversationReply` must handle the new branch — `bot/commands.py::_format_reply` does; the CLI already prints an answer verbatim.

## Rejected alternatives

1. **Route chat vs. search with a heuristic (regex on message length + punctuation) instead of a second LLM tool.** *Rejected.* Multilingual (Spanish/Catalan/English) small talk, thanks, and meta-questions do not have a stable surface signature. The LLM already sits in the interpreter's path; asking it to also pick a tool costs no extra API call.
2. **Keep the single-tool interpreter and add a `chat` variant to `SearchIntent` via a discriminator field on the intent itself.** *Rejected.* Would mix search-query fields (city, categories, radius) with chat-reply fields (answer) on a single Pydantic model; makes downstream `_filter_candidates` and `run_once` more complex without shrinking the router surface.
3. **A third `RoutedMessage` variant `off_topic` (or `refusal`) for messages the bot cannot help with.** *Rejected — deliberately.* A chat reply can already express "that's not something I can help with"; a new variant would require a separate ADR and a new prompt-engineering surface.
4. **Route in the bot layer, not in the interpreter.** *Rejected.* The bot layer has no LLM of its own; a heuristic router would fail on non-English small talk and on ambiguous inputs. The interpreter already has the LLM budget for this.

## Follow-up work (deferred)

- **Ticket #113 (M6)** — teach the Recommender to actively use `save_memory` / `retrieve_memory`. Complements #109/#112 by making the memory tools an active part of the recommendation flow rather than a dormant module.
- **Multi-turn "planazo, and what about tomorrow?" chaining** — beyond ADR 0016's follow-up patterns. A future ticket may add a "modify last query" branch to the router.
- **A separate reasoning trace for `chat` turns** — today a `chat` turn writes zero rows. If operator analytics on "how often does the bot chat vs search?" becomes important, a lightweight audit line (e.g. `var/router_runs.jsonl`) would be a small follow-up.
