<!--
The PR body should be the approved plan from `~/.claude/plans/planazo/…`,
piped in via `gh pr create --body-file`. The sections below are appended by
the executing skill after the plan body — fill them in if you're opening a
PR by hand.
-->

## Summary

<One paragraph — what this PR changes and why.>

## Linked issue

Closes #<N>

## Stages completed

- [ ] Stage 1 — <name>
- [ ] Stage 2 — <name>

## Test plan

- [ ] `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest` — all green
- [ ] <any manual verification steps>

## Docs updated

- [ ] `AGENTS.md`, `README.md`, or `docs/**` sections touched by this change have been rewritten to describe the new state as the only state. No "previously X, now Y".
- [ ] Any new ADR is in `docs/adr/` and referenced from the code / doc that relies on it.

## Reviewer notes

<APPROVE-WITH-NOTES items from `branch-code-reviewer`, if any.>

## Follow-ups discovered

<Bullets — problems in the neighbourhood surfaced by implementers or the reviewer that were NOT fixed in this PR. Each is either filed as its own issue (link it) or explicitly deferred.>
