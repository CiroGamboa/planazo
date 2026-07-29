# ADR 0014: Deterministic ranking boundary

## Status

Accepted

## Context

The Recommender produces validated candidates only after its typed search and
filtering boundary succeeds. The Telegram `/find` surface will later need an
ordered, explainable result without making ranking another untrusted model or
storage boundary.

## Decision

`planazo.rank` is a pure post-Recommender bounded context. Its public
`rank_events` function accepts only validated `Event` candidates, a validated
`SearchIntent`, and `RankingPreferences`; callers invoke it only for an `ok`
`RecommenderResult`. It has no LLM, tool, SQLite, memory, interpreter, monitor,
or bot dependency.

Ranking uses fixed weights and deterministic tie-breaking. The existing catalog
Haversine helper is public so geographic arithmetic has one implementation. A
radius without a trusted origin fails closed. Reasons never expose raw latitude
or longitude; an eligible proximity reason may show only a rounded distance.
This ADR includes a local contract-only example showing that a future consumer
must invoke ranking only for an `ok` result; it is not Telegram integration or
a caller test. Issue #23 owns the real Telegram presentation and wiring.

## Consequences

Ranking is reproducible and independently testable. Future changes to scoring,
reason text, or this public boundary require coordinated compatibility review.
The ranker does not relax Recommender filtering or supply user-interface wiring.
