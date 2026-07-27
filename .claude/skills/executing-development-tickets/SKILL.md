---
name: "executing-development-tickets"
description: "Drive one GitHub issue end-to-end: plan → critic → user approval → stage-by-stage implementation → branch review → PR. Orchestrates the four agents in .claude/agents/. Use when the user says 'work on issue #NN', 'let's tackle #NN', or 'execute the ticket'."
---

# Executing Development Tickets

This skill turns one issue into one merged PR through the fixed pipeline:

```
architect  →  critic  →  USER APPROVAL  →  implementer × N stages  →  reviewer  →  PR
```

You are the orchestrator. You dispatch each agent, gate on the user's approval, and produce the final PR. You do not write plans, code, or reviews yourself — you route.

## Invocation

```
/executing-development-tickets <issue-number> [base_branch=<ref>]
```

or natural language: "execute issue #23", "let's do #14".

**Base branch.** Optional, defaults to `main`. Direct invocations should almost always use the default. `/implement-milestone` passes `base_branch=feat/<milestone-slug>` so per-issue branches stack on the milestone integration branch and per-issue PRs target it rather than `main`. When set, it replaces `main` in Phase 4 (branch creation) and Phase 7 (PR creation). Nothing else changes — the critic, implementer, and reviewer are agnostic to the base branch.

## When to invoke

- User names an issue: "let's work on #23", "execute ticket #14".
- User asks to "pick up the next issue in the milestone".
- After `/writing-development-tickets` files a ticket and the user says "now execute it".

## Preconditions

Before starting:

- The user has named a specific issue (or you've asked which one).
- `gh issue view <N>` succeeds — the issue exists.
- Working tree is clean (`git status`). If not, stop and tell the user.
- You are on the base branch (default `main`, or `<base_branch>` if the caller passed one). If not, `git checkout <base_branch> && git pull --ff-only origin <base_branch>` before proceeding.

## Pipeline

### Phase 1 — Plan

1. Read the issue: `gh issue view <N> --json title,body,labels,milestone`.
2. Launch `system-architect-planner` via the Agent tool. Prompt it with: the issue number and title, the issue body verbatim, and a directive to produce a staged plan at `~/.claude/plans/planazo/<YYYY-MM-DD>-<slug>.md`.
3. Wait for the planner's structured handoff. Expected fields: plan file path, proposed branch, stage count, summary, discovery surprises, discovered issues filed, discovered issues to fix in this PR, risks/open questions.
4. If the planner returns `DISCOVERY-BLOCKER`, surface it to the user and stop. Do not proceed.

### Phase 2 — Critic

1. Launch `plan-critic` via the Agent tool with the plan file path.
2. Wait for its verdict + findings.
3. Route based on verdict:
   - **REVISE** — send findings back to `system-architect-planner` via SendMessage. Loop through Phase 2 until the critic returns APPROVE or APPROVE-WITH-NOTES, or the planner formally rebuts a finding with evidence (in which case, escalate that finding to the user in Phase 3).
   - **APPROVE-WITH-NOTES** — carry the notes into the user approval message.
   - **APPROVE** — proceed.

### Phase 3 — User Approval Gate

Present to the user:

- The plan file path.
- The planner's summary and discovery surprises.
- The critic's verdict and any APPROVE-WITH-NOTES items.
- Any planner rebuttals that the critic didn't accept.

Ask explicitly: "Approve this plan? (or: suggest changes.)"

- If the user asks for changes → send their feedback to the planner via SendMessage → back to Phase 2.
- If the user approves → proceed.

**Do not skip this gate.** Even when critic returns clean APPROVE, the user still names the go/no-go.

### Phase 4 — Branch

Create the branch the planner proposed, branching off the base branch (default `main`, or `<base_branch>` if the caller passed one):

```bash
git checkout <base_branch>
git pull --ff-only origin <base_branch>
git checkout -b <feat|fix|chore>/<slug>
```

Confirm the branch name matches the plan.

### Phase 5 — Implement (stage by stage)

For each stage 1..N in the plan:

1. Launch `plan-stage-implementer` via the Agent tool, **with a fresh context each time**. Prompt: the plan file path and the stage number to implement.
2. Wait for the implementer's structured return: files changed, tests added, docs updated, validation output, divergences, follow-ups.
3. Handle the return:
   - **STAGE-BLOCKER** — send to the planner via SendMessage for a revision. Once the planner returns a revised plan, re-run Phase 2 (critic) → Phase 3 (user) → Phase 5 from the failing stage.
   - **Divergences non-empty** — surface to the user with the implementer's explanation. User decides: accept, or ask the implementer to revise on a fresh context.
   - **Follow-ups non-empty** — accumulate for the PR body. Do NOT ask the implementer to fix them; that violates "one stage only".
   - **Clean return** — proceed to the next stage.

Between stages, do not touch the working tree yourself. The implementer commits.

### Phase 6 — Review

1. Launch `branch-code-reviewer` via the Agent tool with the plan file path and the branch name.
2. Wait for verdict + findings.
3. Route:
   - **REVISE** — for each BLOCKER/MAJOR finding, launch a fresh `plan-stage-implementer` scoped to that finding (prompt: the finding, the file:line, the suggested fix, and the plan section it violates). Then re-run Phase 6 on the updated branch. Loop until APPROVE or APPROVE-WITH-NOTES.
   - **APPROVE-WITH-NOTES** — carry the notes into the PR body.
   - **APPROVE** — proceed.

### Phase 7 — PR

Open the PR against the base branch (default `main`, or `<base_branch>` if the caller passed one):

```bash
gh pr create \
  --title "<type>(<scope>): <subject>" \
  --body-file <plan-file-path> \
  --base <base_branch>
```

Then append to the PR body:

- **Reviewer notes:** the reviewer's APPROVE-WITH-NOTES items (if any).
- **Follow-ups discovered:** the accumulated list from implementers, if any. Note whether each will be filed as its own issue (do it via `gh issue create` before merge).
- **Closes #<N>** — the ticket being executed.

Return the PR URL to the user. Stop. Do not merge; the user or CI does that. When invoked by `/implement-milestone`, that skill drives the CI/merge/bookkeeping steps that follow.

## Rules the Orchestrator Follows

- **One agent at a time.** No parallel implementers on the same branch. No planner+critic in parallel.
- **Fresh context per implementer.** Stage 2's implementer must not inherit Stage 1's implementer context — that's why the pipeline uses one Agent launch per stage rather than SendMessage.
- **Never edit the plan yourself.** Only the planner writes to the plan file.
- **Never skip the user gate.** Approval is not implicit.
- **Never merge for the user.** Merge is a separate, explicit ask.
- **Never `git push --force`.** If a rebase is needed, ask the user first.

## Failure Handling

- **Planner keeps returning DISCOVERY-BLOCKER** — the ticket is mis-scoped. Route the blocker back to `/writing-development-tickets` for a rewrite, or ask the user to split.
- **Critic and planner disagree on the same finding across two rounds** — escalate to the user with both sides in one message. User adjudicates.
- **Reviewer keeps returning BLOCKER on the same finding after two implementer passes** — stop and ask the user. Either the plan is wrong or the finding is wrong.
- **Any command fails unexpectedly (git, gh, validation)** — surface the exact output to the user. Do not attempt destructive recovery (`git reset --hard`, force-push, branch delete) without explicit permission.
