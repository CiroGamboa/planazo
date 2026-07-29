# 0013 — Recommender mutation and clarification boundaries

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** Planazo maintainers

## Context

The Recommender needs to retain explicit user preferences and ask for missing
search details. Both capabilities change the agent's authority: a preference
write must never choose an identity from model-controlled arguments, and a
clarification must not masquerade as a synchronous user reply.

## Decision

`run_once(user_id, intent)` registers `save_preference(key, value)` as a
closure over its caller-supplied user id. The tool never accepts an id, uses
the identity repository rather than raw SQL, and validates the same trimmed,
single-line, bounded `PreferenceRecord` shape before persistence and on its
verification reread. It returns typed preference, unknown-user, store, and
persisted-data outcomes.

The same composition root registers `ask_user(question)`. It records one
validated question for the calling surface without blocking or fabricating a
reply. The first valid call wins; later calls receive
`clarification_already_requested` and cannot overwrite it. A captured question
produces a typed `needs_clarification` Recommender result with no candidates.

Calendar tools remain an explicit caller opt-in and are unrelated to either
boundary.

## Consequences

### Positive

- Model tool calls cannot read or write another user's preferences.
- Preference text has one shared validation contract at write and reread.
- Calling surfaces can continue the conversation without a nested blocking loop.

### Negative / accepted trade-offs

- A run can retain only one clarification question.
- Preference writes verify the typed read boundary after persistence.

### Follow-ups

- Issue #23 owns Telegram capture and rendering of clarification responses.
