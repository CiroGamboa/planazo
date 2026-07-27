# 0004 — Three-store memory model: facts vs. rules, private vs. shared

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** dvetencourt

## Context

`docs/MVP-ARCHITECTURE.md` §8 and §"HW2 memory model" call for a non-relational store for
free-form things the agent learns about a user (facts, cued for later retrieval; event-scoped
notes) and a markdown store for operator-editable rules that get pushed into every agent run, on
top of the relational store ADR 0003 just decided. Two things need a code-shape guarantee, not a
prompt-discipline promise (`AGENTS.md` rule 2 and rule 3's spirit extended to memory):

1. **A user's private facts must never be readable by another user's session**, even though both
   sessions run the same agent code against the same tool names.
2. **Content written by one user and read back by another (a shared note) is untrusted data**, on
   the same footing as scraped web content — it must never be able to steer the reading agent's
   behavior just because it now arrives via a memory tool instead of a scraped page.

Alternatives considered:

- **A single `memories` table/file with a `scope` column, filtered by a `user_id` argument the tool
  call supplies.** Rejected — this is the shape that fails (1). If `user_id` is a parameter the
  model's tool-call arguments carry, a confused or adversarial prompt can supply someone else's
  `user_id` and the *filter*, not the *code*, is the only thing standing between it and another
  user's private facts. A filter is prompt-adjacent discipline, not a code-shape guarantee.
- **Embeddings + cosine similarity for cue matching now.** Rejected for v1 per
  `docs/MVP-ARCHITECTURE.md`'s own scoping ("token overlap is fine for v1; embeddings are a
  follow-up ADR") — no embedding model is wired anywhere in this repo yet, and the three demo
  traces this ticket ships don't need ranked similarity, just "does this fact resurface on a
  plausible cue."
- **One JSON file per user containing both facts and notes.** Rejected: facts (cued, private-or-
  shared, not tied to any one event) and notes (event-scoped, free-form) are different retrieval
  shapes — matching on cue vs. matching on `event_id` — mixing them into one file forces every
  reader to filter by record type instead of the storage layout doing it for free.

## Decision

**Two JSON docstore files per scope directory**, `facts.jsonl` and `notes.jsonl`, under
`agent/var/memory/{private/{user_id}/, shared/}`. `Fact` (`cue: str`, `content: str`, `scope`,
`author_user_id`, `created_at`) and `Note` (`event_id: str`, `content: str`, `scope`,
`author_user_id`, `created_at`) are Pydantic v2 models in `planazo/schemas/memory.py` — every
append is validated before it touches disk, per `AGENTS.md` rule 1. Cue matching is token-overlap
only (lowercase, `\w+`-tokenize `query` and each fact's `cue`, non-empty set intersection = match)
— no ranking, no embeddings; a follow-up ADR is required before embeddings replace this.

**Scope is structurally binary, never a third option.** `scope` is `Literal["private", "shared"]`
on every write and `Literal["private", "shared", "both"]` on every read — there is no
`"someone else's private"` value that could ever exist in the type, so a write can only land in the
caller's own private directory or the one shared directory, and a read can only ever touch the
caller's own private directory plus the shared one.

**`user_id` is validated as an integer before it selects a directory.** Structural scoping is only
as strong as the value that picks the directory: `Path("var/memory/private") / str(user_id)` with
`user_id="1/../2"` normalizes to `var/memory/private/2/` — another user's private directory. Writes
alone being Pydantic-validated (`Fact.author_user_id: int`) is not enough, because the *read* path
picks a directory too. So every `memory/facts.py` entry point resolves its `(user_id, scope)` pair
through a `MemoryScopeRequest` Pydantic model (`user_id: int = Field(ge=1)`) before touching disk —
a traversal-shaped id is a `ValidationError`, not a directory. This matters concretely because
issue #3's bot derives `user_id` from a Telegram-supplied identifier: it is exactly the
external-payload-crossing-into-persisted-state case `AGENTS.md` rule 1 governs.

**`user_id` is bound by the caller, never accepted from the model.** `memory/api.py` exposes
`retrieve_memory`/`save_memory`/`retrieve_notes`/`save_note` as tool names, but the actual
functions registered in a given run's `TOOL_REGISTRY` are nested closures built by
`build_memory_tools(user_id: int)` — each closure's own signature has no `user_id` parameter at
all (it is a free variable captured from the enclosing scope, not a parameter `schema_for` can see
or a key the model's tool-call arguments can override). If a tool call's arguments happen to
include a `user_id` key anyway, dispatch raises `TypeError` (unexpected keyword argument), which
`planazo.agents.loop.run_loop`'s existing dispatch try/except already turns into a
`tool_failure_result` marker — a hard failure, never a silent scope override. This is the same
"enforced by code shape, not by prompt discipline" pattern `docs/MVP-ARCHITECTURE.md` already uses
for the Extractor's caption boundary, applied to identity instead of content. The JSON Schema sent
to the model (`additionalProperties: false`, no `user_id` property) is a second, independent line
of defense — the provider's own schema enforcement — but the closure's `TypeError` is the guarantee
this ADR actually relies on.

**Every memory tool returns a typed error state, never a bare exception.** The four tool wrappers
in `memory/api.py` catch `ValidationError` from the underlying `facts.py` call and return
`{"error_type": "invalid_memory_data", "message": str}` on a write or
`{"error_type": "invalid_memory_query", "message": str}` on a read — the same two-layer pattern
`tools/tools.py` already uses (the tool owns input-validation branches; `run_loop`'s dispatch
try/except owns everything else as a `tool_failed` marker). A model that emits `scope="global"` or
an empty `cue` gets a readable typed branch it can correct on the next turn, not an opaque failure.

**Shared content is data, never instruction, by the same mechanism the loop already has.** A note
or fact's `content` field, however it was written, only ever reaches the reading agent as the
JSON-serialized return value of a tool call (`function_call_output` in the Responses-API message
list) — it is never concatenated into the system message or any instruction-bearing role. This
requires no new code: `run_loop` already only ever appends tool results as `function_call_output`.
The three demo traces this ticket ships prove it empirically (`agent/scripts/demo/`); the automated
suite proves it structurally, with a negative assertion — a run whose stored fact carries a
distinctive sentinel string asserts that sentinel appears in a `function_call_output` message and in
**no** message whose `role` is `"system"`, across every `call()` invocation of the run.

**Rules are pushed, always, from disk.** `memory/rules.py::load_rules() -> str` reads the
module-level `RULES_DIR` constant (default `Path("data/rules")`, resolved relative to the `agent/`
working directory the same way `tools/tools.py`'s `CANDIDATES_PATH` already is — a bound default
parameter would freeze the path at function-definition time and break test monkeypatching, so the
lookup happens inside the function body, not in the signature) and concatenates every `*.md` file
under it (sorted by filename for determinism) into one string, re-read on every call — no caching,
no code change required to alter agent behavior, only an edit to a committed markdown file.
`planazo.agents.loop.run_loop` gains an
optional `system: str | None` parameter (prepended once as the first message when not `None`);
`event_agent.run_once` assembles it from `load_rules()` plus, when a `user_id` is supplied, that
user's preference rows.

## Consequences

### Positive

- Private-vs-shared is provably correct by inspecting the directory-scoping code once, not by
  auditing every prompt that might leak a `user_id` — four unit tests lock it at the cheapest
  possible tier: private isolation, shared visibility, the closure's `TypeError` on an injected
  `user_id`, and a traversal-shaped `user_id` being rejected rather than resolved (the last one
  closes the hole the `user_id`-validation decision above exists to prevent, so it is not optional).
- An operator changes agent behavior by editing a markdown file and re-running — no deploy, no code
  review for a rules tweak (though a rules *file* PR still goes through normal review).
- The untrusted-shared-content property costs zero new code — it is a direct consequence of
  `run_loop`'s existing message-role discipline, which this ADR documents rather than reinvents.

### Negative / accepted trade-offs

- Token-overlap cue matching over-surfaces: a fact cued `"music"` resurfaces on any query
  containing that word, with no relevance ranking. Acceptable per the architecture doc's own bar
  ("manual review shows no obviously-wrong surfacing across the three demo traces"); embeddings are
  an explicit follow-up ADR, not silently smuggled in as "a better tokenizer."
- JSONL, append-only, no index. Fine at demo scale (a handful of facts/notes per user); revisit if
  a user's fact count ever makes a full-file scan on every `retrieve_memory` call slow enough to
  matter.
- `search_events`/`save_event` (ADR 0003) are *not* scoped by this ADR's private/shared model —
  events are the shared domain surface, not per-user memory. Anyone reading this ADR looking for
  why `search_events` has no `user_id` at all should look at ADR 0003 instead.
- **CLI identity is unauthenticated.** `planazo-agent --user-id 2 "..."` binds whatever identity the
  shell says, so a local operator can read any user's private facts through the agent. This is
  deliberately not treated as a hole in the guarantee above, because that guarantee is scoped to
  what a *model or prompt* can reach across sessions: the same local operator can already
  `cat var/memory/private/2/facts.jsonl`, so the flag grants no privilege the filesystem does not.
  `--user-id` is a development affordance for exercising the memory tools and the rules push from
  the CLI. The authenticated mapping — a Telegram identity resolved to `users.id` before any tool is
  bound — is issue #3's `bot/session.py`, and it is the only surface that should ever face a real
  user.

### Follow-ups

- Embeddings-based cue matching: its own ADR, superseding this one's "token overlap" clause only,
  once a concrete relevance problem (not just "it could be better") shows up in practice.
- `save_preference` (writing preference rows from chat) is out of this ticket's scope — the
  Interpreter/bot ticket adds it; this ADR's push-context assembly only *reads* preference rows.
- The Extraction Agent (separate ticket) never imports `memory/facts.py` or `memory/api.py` — it
  only ever writes to `events` (ADR 0003) and reads rules; facts/notes are the Recommender's
  channel to a user's history, not the Extractor's.
