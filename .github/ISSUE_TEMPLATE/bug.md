---
name: Bug
about: Something behaves differently from what the spec, ADR, or a prior ticket says it should.
title: "fix: "
labels: ["fix"]
---

## Observed

<What happens today — concrete inputs → wrong output or crash. Include wire payload / stack trace / screenshot if available.>

## Expected

<What should happen instead. Cite the spec section, ADR, or ticket that establishes the expectation.>

## Reproduction

1. <step>
2. <step>
3. <observed>

## Acceptance criteria

- [ ] A regression test exists that fails on the current `main` and passes after the fix.
- [ ] <any user-visible fix criterion>

## Out of scope

- <adjacent problems this ticket will not fix>
