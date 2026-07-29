# Memory resurface demo — issue #113

- **Date:** 2026-07-29
- **Ticket:** #113 — `feat(agents): teach the Recommender to actually use memory tools`
- **Rules file added:** [`data/rules/010-memory-usage.md`](../../data/rules/010-memory-usage.md)
- **Model:** CHEAP tier (OpenCode Zen), via `agentlib.core.call`

This is the demo ADR 0004 calls for: a fact saved in one Recommender turn is
retrieved and used in a later, unrelated turn, with no code change — only a
new `data/rules/*.md` file, picked up automatically by
`memory.rules.load_rules()`'s sorted-glob concatenation.

## Why a preference seed, not a chat transcript

`event_agent.run_once(user_id, intent)` has no raw-user-message parameter —
the Recommender's own LLM turn always opens with the fixed
`RECOMMENDER_WORK_MESSAGE` constant ("Find events matching the validated
search intent."). The only per-run channel that carries "something this user
told the system in an earlier turn" into the Recommender's pushed context is
the preferences block (`_preferences_text`), which is exactly what a real
production turn looks like after `conversation/service.py`'s
`_handle_clarification_answer` records a `set_preference(...)` row. So both
the automated live test and this demo seed a durable preference the same
way: `identity.set_preference(conn, user_id, "venue_preference", "avoids
loud, crowded venues and prefers something quieter")`, then run two
Recommender turns.

## Reproduction

```bash
uv run pytest -m live tests/agents/test_memory_resurfaces_live.py -v -s
```

Requires a real `OPENCODE_API_KEY` (loaded from `.env` via `find_dotenv()`).
Without a live key, the mocked-plumbing equivalent (same fact-persists/
resurfaces claim, scripted LLM) is:

```bash
uv run pytest tests/agents/test_memory_resurfaces.py -v
```

## The wording had to be iterated on twice — once for reliability, once for budget

**Round 1 — the first draft never fired.** An earlier draft compressed the
save trigger into one dense conditional sentence ("Before `search_events`
you must `save_memory` any preference above not yet retrieved, or
asked-to-remember or repeated-twice; never noise, never `save_preference`.").
On measurement, that wording made the real CHEAP model call `retrieve_memory`
reliably but **never** call `save_memory` — 0 saves across 5 direct live
runs, and the automated live test's own built-in 3-attempt retry failed
outright. Root cause: the sentence's "preference above" pointed the wrong
way — the pushed preferences block actually renders *after* the rules text
in the system message, not above it — and the run-on conditional was too
much to parse for a cheap-tier model.

**Round 2 — the fix that worked blew the context budget.** Rewriting the
instruction as an explicit five-step checklist (naming "User preferences" as
a literal, separate store `retrieve_memory` does not read automatically, and
giving the `save_preference` prohibition its own sentence) fixed reliability
completely: 5/5 direct live runs, 4/4 two-turn runs with a clean single save,
2/2 live-pytest passes. But that checklist was 174 words, and
`tests/test_memory_rules.py::test_seed_rules_stay_within_the_context_budget`
caps *all* committed `data/rules/*.md` files combined at 120 words / 10
non-blank lines — a guardrail earned from a prior ticket's measurement that
extra system-message prose measurably suppresses gated-tool-call rates.
`data/rules/000-core-rules.md` already spends 84 of those 120 words, leaving
only 36 words / 6 lines for this file — which is exactly what the original
(broken) draft had been hand-tuned to fit, explaining why reliability had
been sacrificed for budget in the first place.

**Round 3 — compressing back into 36 words without losing what round 2 fixed.**
Word-budget-constrained candidates were live-tested directly (bypassing
pytest's retry loop) to measure `save_memory` trigger rate per wording, e.g.:

| candidate | words | turn-1 saves | notes |
|---|---|---|---|
| dense original | 34 | 0/5 | directionally wrong + too dense |
| ordered, negatives last | 36 | 1/5 | 2/5 wrong-tool (`save_preference`) instead |
| "Cue every X line... Never save_preference." | 36 | 2/5 | |
| isolated imperative sentence | 36 | 3/6 | |
| combined single bullet | 32 | 2/6 | 1 wrong-tool |
| + "unrepeated" qualifier | 34 | 0/6 | that specific word made it worse |
| **final (current file)** | 34 | 13/14 (92.9%) | 2 harmless extra `save_preference` calls, 0 missed saves |

The wording that finally worked reliably within budget names the preferences
block and the required tool call as one unconditional two-clause imperative
("you must: `retrieve_memory` ... then `save_memory` each `"User
preferences"` line, before `search_events`. Never `save_preference`."),
keeping the explicit-remember/twice-repeated/never-noise/shared-attribution
rules on a second bullet. This is the version committed in
`data/rules/010-memory-usage.md` (34 words, 2 lines — well inside the 36-word
/ 6-line ceiling this file has).

**Known trade-off, documented rather than hidden.** The round-2 (over-budget)
174-word checklist also produced a clean single save on turn 2's follow-up
run in 4/4 ad hoc trials — i.e. it never redundantly re-saved a fact the
model already had. The final in-budget wording does not fully reproduce
that: across ad hoc two-turn trials, turn 2 re-called `save_memory` for the
same already-known fact in roughly half of the runs (a harmless but
redundant extra write, not a missed save or a wrong-tool call). Neither the
mocked test nor the live test in this PR assert turn-2 save-suppression —
they assert that the fact is retrievable and reflected in the answer, which
holds regardless — so this is called out here as an honest, known limitation
of fitting reliable save-triggering into a 36-word ceiling, not something
papered over by loosening a test. If a future ticket needs strict turn-2
idempotency, treat it as a follow-up: either raise the budget or de-duplicate
`save_memory` writes in `memory.facts` itself rather than relying on prose.

## Actual live run (verbatim, captured this session, final wording)

```
[live memory] save_memory called on attempt 1/3
[live memory] save_memory args: {'cue': 'venue_preference avoids loud crowded prefers something quieter', 'content': 'Prefers events at quieter venues and wants to avoid loud, crowded venues.', 'scope': 'shared'}
[live memory] turn 1 answer: 'I searched for events in **Barcelona** starting after **2026-07-30T12:41:49Z** (with your preference for **quieter, less crowded venues**), but **no matching events were found** in the available listings.'
[live memory] turn 2 answer: 'I checked for events in **Barcelona** starting after **2026-07-30** (up to your validated end time of **2026-08-06**), and I’m not seeing any matching events in the available listings right now.

If you tell me **what kind of events** you want (e.g., concerts, comedy, talks, club nights, stand-up, art/exhibitions), I can try a more targeted search.'
PASSED
```

`save_memory` fired on the first attempt (no retry needed this run). The
test's mechanical assertion (not just the prose above) confirms the
resurface: turn 2's `retrieve_memory` call returned a fact whose `content`
field is byte-identical to what turn 1's `save_memory` call saved — read
back from the isolated `facts.jsonl` under `tmp_path`, not paraphrased by the
model.

## What this rules out

- **`save_preference` is still registered** (issue #113 depends on #109,
  which has not landed) and is a superficially plausible wrong tool for a
  preference-shaped fact. Across the reliability measurements in the table
  above, the final wording produced only 2 redundant `save_preference` calls
  out of many runs (never a *missed* `save_memory`), and none in the captured
  live-test run above.
- **Step budget is not the bottleneck.** `max_steps=6` gives room for
  `retrieve_memory` → `save_memory` → `search_events` in one turn — the
  fix that mattered was the rule's wording, not a larger step or token
  budget.

## Cross-references

- [ADR 0004 — Three-store memory model](../adr/0004-three-store-memory-model.md)
  — the shared/private/notes shape this rule pushes discipline for, and the
  source of the demo requirement this file satisfies.
- [`tests/test_memory_rules.py`](../../tests/test_memory_rules.py) — the
  120-word / 10-line context-budget guardrail this file had to fit inside.
- [`tests/agents/test_memory_resurfaces.py`](../../tests/agents/test_memory_resurfaces.py)
  — mocked-LLM plumbing proof (fact persists across an unrelated turn; ADR
  0004 cross-user private/shared/injection invariants through the real loop).
- [`tests/agents/test_memory_resurfaces_live.py`](../../tests/agents/test_memory_resurfaces_live.py)
  — the live test this trace was captured from.
