# Session 6 submission — Ana Karla Caballero González

**Primary individual commit:**
[0abba09 — typed Recommender executor](https://github.com/CiroGamboa/planazo/commit/0abba09)

## What I built

My primary contribution turns Planazo’s Recommender into a typed, safe product
boundary: `run_once(user_id, intent)` accepts a validated search intent and
returns a `RecommenderResult` instead of an unstructured answer. It performs
preflight validation for preferences and trusted search origin, searches only
the validated SQLite catalog, filters candidates deterministically, and returns
clear branches for errors, no results, or a clarification request. The CLI and
runtime interfaces use the same contract.

I also added the deterministic ranker in
[3f24d0d](https://github.com/CiroGamboa/planazo/commit/3f24d0d): it ranks
validated candidates with explicit weighted rules and user-facing reasons,
without asking the LLM to choose an order. In
[c34fd19](https://github.com/CiroGamboa/planazo/commit/c34fd19), I reinforced
the trusted-origin boundary for radius searches: the LLM cannot invent
coordinates, and a radius request without an application-owned origin becomes a
typed error rather than an unfiltered search.

## Session 6 ideas in the group product

These contributions support the product surface demonstrated by our group:

- A Telegram bot receives messages through a validated channel and keeps each
  user’s work in a bounded FIFO queue.
- The scheduler runs independently of user messages and records silence
  decisions (for example, a source that is not due) with a named reason.
- The Catalog Curator is a separate, privileged admin agent. It can maintain
  stale, duplicate, or misclassified catalog entries; the normal Recommender
  cannot use those tools.
- Typed errors, deterministic filtering/ranking, and trusted geographic origin
  make the channel safer: an uncertain input never becomes a plausible but
  untrustworthy recommendation.

## How I tested it

I added/updated contract tests for the Recommender, catalog preflight behavior,
and deterministic ranking: [event-agent tests](tests/test_event_agent.py),
[radius/preflight tests](tests/test_issue59_preflight.py), and
[ranker tests](tests/test_rank.py). The product-level channel and background
paths are covered by [queue tests](tests/test_bot_queue.py),
[scheduler tests](tests/test_scheduler_service.py), and
[curator tests](tests/test_curator_agent.py). We run:

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

For the presentation, [HOMEWORK_DEMO_GUIDE.md](HOMEWORK_DEMO_GUIDE.md) links
the channel, queue, trigger, silence branch, admin subagent, ADRs, and tests
to the exact implementation files.
