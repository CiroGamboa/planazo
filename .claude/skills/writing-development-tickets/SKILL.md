---
name: "writing-development-tickets"
description: "Scope and file one well-formed GitHub issue for Planazo. Use when the user says 'let's open an issue for X', 'file a ticket', 'write up the Y work', or wants to draft the batch of issues that follows a milestone plan. Produces a single issue via `gh issue create` following the repo's feature/bug template."
---

# Writing Development Tickets

This skill turns a rough idea into a single GitHub issue that the executing pipeline can pick up without follow-up questions. One issue, one intent, one "done".

## When to invoke

- User says "let's write an issue / ticket for X".
- User is producing a batch of issues to seed a milestone.
- Planning surfaced a follow-up that should be tracked separately.

Do NOT use this to drive execution — that's `/executing-development-tickets`.

## Non-negotiable shape

Every issue produced by this skill has:

1. **A single intent.** If "and" appears in the title, split it.
2. **A defined done.** Acceptance criteria in bullet form, each independently verifiable.
3. **Explicit out-of-scope.** What this ticket will *not* do. Prevents scope creep during execution.
4. **A link to context.** The relevant section of the product context doc, the relevant ADR, or the parent issue.
5. **A label + milestone** when the user names one.

## Workflow

### 1. Understand the intent

Ask *only* if you cannot answer these from the conversation:

- What user-visible or system-visible behaviour will exist after this ticket that doesn't exist today?
- Which module or product area does this belong to?
- Is this a `feat`, `fix`, or `chore`? (Chores are anything with no user-visible effect.)
- Are there upstream tickets that must land first?

If the user's ask spans multiple intents, name the split explicitly: "I'll file three issues — <A>, <B>, <C>. Confirm?" Do not silently narrow.

### 2. Read the relevant context

Before drafting the body: read the relevant product context doc, any ADRs in `docs/adr/` that touch this area, and any related open issues (`gh issue list --search "<keywords>"`). The ticket body cites what's authoritative.

### 3. Draft the ticket

Two templates — pick the one that matches:

#### Feature ticket

```markdown
## Problem

<1–3 sentences — what limitation exists today, or what user need is unmet>

## Motivation

<Why this matters now. Link to the product spec section or the ADR / parent issue.>

## Proposed outcome

<What exists after this ticket ships. User-visible behaviour first, then the surface (API endpoint, schema field, bot command). No implementation details — those come from the planner.>

## Acceptance criteria

- [ ] <criterion 1 — independently verifiable>
- [ ] <criterion 2>
- [ ] <criterion 3>

## Out of scope

- <thing this ticket will not do — one bullet per>

## Notes

<Optional: known constraints, related tickets, hints for the planner. Never a solution sketch — that's the planner's job.>
```

#### Bug ticket

```markdown
## Observed

<What happens today — concrete inputs → wrong output or crash. Include the wire payload / stack trace / screenshot if you have one.>

## Expected

<What should happen instead. Cite the spec section that establishes the expectation.>

## Reproduction

1. <step>
2. <step>
3. <observed>

## Acceptance criteria

- [ ] A regression test exists that fails on the current `main` and passes after the fix.
- [ ] <any user-visible fix criterion>

## Out of scope

- <adjacent problems this ticket will not fix>
```

### 4. File the issue

Show the drafted body to the user and confirm before filing. Then:

```bash
gh issue create \
  --title "<type>: <slug>" \
  --body-file <path/to/drafted-body.md> \
  --label "<type>" \
  --milestone "<milestone or omit>"
```

Title format: `<type>: <short imperative>` — e.g. `feat: add /start command to Telegram bot`, `fix: /help crashes on empty message`. Under 72 chars.

After filing, return the issue number and URL to the user.

## Batch mode

When the user is seeding a milestone with several issues:

1. Draft *all* of them first as a numbered list — do not file yet.
2. Present the batch: title + one-line summary each.
3. Ask the user to confirm the batch, edit any titles, and name the milestone.
4. File them in order, capturing each issue number, so later issues in the batch can reference earlier ones ("depends on #NN").

## Anti-patterns

- **Solution in the ticket body.** The ticket says *what* and *why*, never *how*. Implementation is the planner's job.
- **"Investigate X".** Not a ticket. Either it's a bug (write a bug ticket) or a chore ("document current behaviour of X").
- **Multi-intent titles** (`feat: add command and update schema and refactor router`). Split.
- **Missing "out of scope".** Without it, execution creeps. Always include, even if it's "None."
- **Copying context doc verbatim.** Link to the section; don't restate it.
