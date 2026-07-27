---
name: implement-milestone
description: >
  Drive a whole GitHub milestone end-to-end on a dedicated integration branch:
  sequence issues, run the executing-development-tickets pipeline per issue with
  a relaxed approval gate, squash-merge PRs into the integration branch, keep
  issue state current, file prioritized follow-ups, and — once every issue is
  closed — prepare a single consolidation PR from the integration branch to
  `main` with a rich design writeup, then stop and hand off to the user for
  review and merge.
  Triggers on: "implement milestone N", "/implement-milestone <N|URL>",
  "execute milestone N", "finish the milestone".
---

# Implementing a Milestone

Main-process orchestrator one level above `/executing-development-tickets`. One milestone = many issues = many per-issue PRs stacked on a single **integration branch** `feat/<milestone-slug>`, driven serially and merged into that branch without stopping between issues. When every issue is closed, this skill opens **one consolidation PR** from `feat/<milestone-slug>` to `main` — with a design walkthrough, a decision/rationale table, and a Mermaid diagram — and stops there for human review. **Main never implements issues inline** — every issue goes through the executing-development-tickets pipeline (architect → critic → stage implementers → reviewer); this skill owns the loop around it: sequencing, merging into the integration branch, bookkeeping, post-merge validation, escalation, and the final consolidation PR.

## Invocation

```
/implement-milestone <milestone-number-or-URL>
```

or natural language: "implement milestone 3", "finish milestone https://github.com/CiroGamboa/planazo/milestone/3".

## Standing permissions

Invoking this skill **is** the user's explicit grant to, without re-asking:

- Run the `/executing-development-tickets` pipeline per issue, including all its subagents.
- Create, update, label, re-scope, and close GitHub issues; file follow-up issues; assign issues to the milestone; comment on issues.
- Create the milestone **integration branch** `feat/<milestone-slug>` off `main`, push it, and force-push it during in-milestone rebases against `main` (only this skill writes to a milestone integration branch).
- Create per-issue feature branches off the integration branch; commit; push; rebase feature branches on the integration branch.
- Open per-issue PRs against the integration branch and **squash-merge them once tests are green** (`gh pr merge <N> --squash`).
- Rebase the integration branch on `main` at the end of the milestone and **open** the consolidation PR from the integration branch to `main`.
- Close the milestone when the user has merged the consolidation PR.

**Never granted — always stop and ask:** publishing packages, force-pushing or committing directly to `main`, deleting the milestone, merging on red tests, and — critically — **merging the consolidation PR into `main`**. The consolidation PR is prepared but never auto-merged; the human reviews and merges it.

## Step 1 — Intake

1. Resolve the milestone: `gh api repos/:owner/:repo/milestones/<N>`. List its issues (open and closed): `gh issue list --milestone "<title>" --state all --json number,title,state,labels`.
2. Read every open issue in full — the milestone description + the issue bodies are the SSOT for scope and sequencing.
3. Build the execution queue. Ordering: hard dependencies first (per each ticket's "Notes → Dependencies coming in"), then the smallest/most-independent items to warm up the pipeline, then the higher-risk items. Pull the keystone / highest-uncertainty issue early so design problems surface while everything is still cheap to change.
4. Derive `<milestone-slug>`: kebab-case of the milestone title after any `M<N>:` prefix, ≤ 30 chars, alnum + `-` only. Examples: "M2: Instagram source adapter" → `instagram-source`; "M6: /find handler wiring" → `find-handler`.
5. Post the queue **and the chosen slug** to the user as a status message (ordering + anything that looks stale or mis-premised + the integration-branch name `feat/<slug>`) and **proceed** — this is informational, not an approval gate. The user interrupts if they disagree.

## Step 2 — Integration branch

1. `git fetch origin`. Working tree must be clean; if dirty, ask before mutating.
2. If `origin/feat/<slug>` **exists**:
   - Adopt it: `git checkout feat/<slug> && git pull --ff-only origin feat/<slug>`.
   - Compare it to `origin/main`: `git log --oneline origin/main..feat/<slug>` — the commits should look like prior per-issue squash-merges of this milestone. If it looks like an unrelated branch, stop and ask.
   - Look for `~/.claude/plans/planazo/milestone-<N>-integration.md` — if present, adopt it as the running design journal. If missing, seed it (Step 2.4) from the milestone description and existing squash-merge commit messages.
3. If `origin/feat/<slug>` **does not exist**:
   - `git checkout main && git pull origin main`.
   - `git checkout -b feat/<slug>`.
   - `git push -u origin feat/<slug>`.
4. **Bootstrap the running design journal.** Create `~/.claude/plans/planazo/milestone-<N>-integration.md` (a scratch file outside the repo — never committed) with:
   - `# Milestone <N>: <title>` header + link to the milestone.
   - Empty stubs for the final-PR sections: `## TL;DR`, `## Impact on existing code`, `## Design walkthrough` (with a placeholder ```mermaid``` block), `## Key design choices` (empty Decision / Rationale table with header row), `## Suggested review order`, `## Verification`, `## What's next`.
   - Write the seed to the scratch file — do NOT commit it. No commits land directly on the integration branch: every change arrives via a squash-merged per-issue PR, and the final rebase in Step 4 adds none.

## Step 3 — Per-issue loop

For each issue, **serially** (one branch / one PR in flight at a time — the working tree is shared and the per-issue pipeline is already multi-agent):

1. **Sync:** `git checkout feat/<slug> && git pull --ff-only origin feat/<slug>`. Working tree must be clean.
2. **Run the executing-development-tickets pipeline** with `base_branch=feat/<slug>` — per-issue branches branch off the integration branch and per-issue PRs target it. The pipeline drives through PR creation, with the milestone-mode deltas below.
3. **Wait for checks (if any) + merge.** If the repo has CI configured, `gh pr checks <PR#> --watch`; merge only on green. If no CI is configured yet, run the full local test suite (`cd agent && uv run ruff check && uv run mypy src && uv run pytest`) on `feat/<slug>` after the merge would land; merge only if all green. Then confirm the PR body says `Closes #<issue>`, then `gh pr merge <PR#> --squash` (target: the integration branch). Verify the issue auto-closed; close it manually with a one-line evidence comment if not.
4. **Bookkeeping.** On each closed issue, add a one-line comment: `Merged via PR #<pr> (<squash-sha>) into feat/<slug>. Follow-ups filed: #a, #b.` The GitHub auto-close comment from `Closes #<issue>` is not enough — the follow-ups line is load-bearing for later sessions.
5. **Append to the design journal.** In `~/.claude/plans/planazo/milestone-<N>-integration.md`, append: (a) a one-paragraph "what this issue delivered" note, (b) 1–3 rows for the Decision / Rationale table drawn from the per-issue PR's "Design choices" section, and (c) any new "Concepts introduced" one-liner. Write to the scratch file only — do not commit it anywhere.
6. **Post-merge validation.** `git checkout feat/<slug> && git pull`, then re-run the full test suite on the integration branch. A broken integration branch is never acceptable: fix it before starting the next issue. If the shipped behaviour touches a real external surface (LLM call, Telegram, Instagram), run one targeted smoke — cheap, minimal — before moving on.

### Milestone-mode deltas to executing-development-tickets

Everything in that skill applies except:

- **Base branch: pass `base_branch=feat/<slug>`.** The pipeline branches per-issue feature branches off it and targets per-issue PRs at it (see `/executing-development-tickets` for the exact wiring).
- **Target confirmation: don't ask the user.** Validate the issue premise against the milestone description and the current behaviour instead. Ticket premises rot — when evidence contradicts one, update or close the issue with the evidence attached and move on. Escalate only if the correction materially changes milestone scope.
- **Plan approval: the critic loop is the gate.** Auto-approve when `plan-critic` returns `APPROVE` or `APPROVE-WITH-NOTES` and your own read of the plan is clean. Escalate to the user only for: critic↔architect deadlock; `DISCOVERY-BLOCKER` that evidence can't resolve; plans that break a compatibility surface (data contracts named in `AGENTS.md`, tool boundaries, approval-gate policy) or have irreversible data effects.
- **Hand-off: don't stop at the per-issue PR.** Drive checks → merge into `feat/<slug>` → bookkeeping → journal update → post-merge validation per the loop above.

## Step 4 — Prepare the consolidation PR

Runs once the milestone has zero open issues.

1. **Rebase the integration branch on `main`.** `git checkout feat/<slug> && git fetch origin && git rebase origin/main`. Squash-merged per-issue commits stay individual on the integration branch — the rebase only shifts them onto the current `main` tip so the final PR presents as linear history. If the rebase conflicts, resolve inside `feat/<slug>` (never on `main`); push with `--force-with-lease` since only this skill writes to the integration branch. If conflicts are non-trivial, stop and ask.
2. **Finalize the design journal.** Complete every stub in `~/.claude/plans/planazo/milestone-<N>-integration.md`:
   - **TL;DR** — one paragraph naming the compatibility impact (or lack thereof) and ending with the roll-up of every `Closes #<n>` in the milestone.
   - **Impact on existing code** — reviewer-safety framing: near-term / today / longer-term commitments, safety guards, follow-up ticket links.
   - **Design walkthrough** — required ```mermaid``` block. Pick the shape that fits (flowchart for architecture, sequence for interactions, state for lifecycles). Do not omit for architectural milestones.
   - **Key design choices** — Decision / Rationale markdown table, one row per accepted decision, sourced from the appends made in Step 3.5.
   - **Suggested review order** — numbered list of the per-issue PRs (with squash SHAs), ordered to make the story readable start-to-finish.
   - **Verification** — test suite pass counts, post-merge validations that ran, and any lanes deliberately skipped with a reason.
   - **What's next** — one or two sentences of forward links to follow-up issues or the next milestone.
   - Finalize the scratch file in place — do not commit it.
3. **Draft the PR body.** The consolidation PR body **is** the design journal — copy the file contents verbatim into the PR body (skipping only the file's `# Milestone <N>:` header line, since `gh pr create --title` supplies the PR title). Follow the discipline in `pr-templates.md` next to this file.
4. **Open the PR.**
   ```bash
   gh pr create \
     --base main \
     --head feat/<slug> \
     --title "feat(<scope>): <milestone title>" \
     --body-file ~/.claude/plans/planazo/milestone-<N>-integration.md
   ```
   The `<scope>` follows Planazo's conventional-commits convention (see `AGENTS.md` §Commit style) — the module the milestone predominantly touches (e.g. `bot`, `agents`, `sources`).
5. **Hand off and stop.** Post to the user: PR URL, one-line summary, follow-ups filed with priorities (implemented vs deferred), post-merge validations run, and the reminder that human review is the gate here. **Do not merge.** **Do not close the milestone yet** — that happens after the user merges the PR, on the user's cue.

The full template with placeholders lives in `pr-templates.md` next to this file.

## Follow-ups and discovered work

File everything you discover (bugs, improvements, deferred edge cases) as issues per `writing-development-tickets` conventions — label always, priority stated in the body. Then triage:

- **High-priority and in-scope** → assign to the milestone and insert into the queue.
- **Low-priority or out-of-scope** → file, cross-link from the source issue/PR, leave for later. Note it in the final report and in the design journal's `## What's next` section.

Never let discovered work die in a PR comment or the conversation — if it isn't an issue, it didn't happen.

## Durable state — the session will outlive its context

A milestone run is long; the conversation will be compacted. The durable record lives in GitHub and on the integration branch, never in the conversation:

- **Per-issue closed-comment lines** are the merge record (Step 3.4) — one per merged issue, with the PR number, squash SHA, and follow-ups filed.
- **`~/.claude/plans/planazo/milestone-<N>-integration.md`** — a scratch file outside the repo, persistent across sessions — is the running design journal (Step 2.4 and Step 3.5). It accumulates as issues merge and *becomes* the consolidation PR body in Step 4; it is never committed.
- **Issue state** (open/closed/labels/milestone) is always current — stale bookkeeping is a bug, not cosmetics.
- **Per-issue plans** live in `~/.claude/plans/planazo/` (scratch, outside the repo); their durable record is the per-issue PR body that embeds them.

After compaction or an interrupted session, rebuild state from: the milestone page + per-issue closed comments + `git log feat/<slug>` + the running design journal + `gh pr list --base feat/<slug>` — never from what you remember of the conversation.

## Escalation — the only reasons to stop and ask

1. A compatibility-surface break (data contract, tool boundary, approval-gate policy) where the plan forces a choice.
2. A genuine design fork the milestone description and existing ADRs are silent on, where the options materially diverge.
3. `main` broken in a way you didn't cause and can't fix, blocking all progress.
4. The integration branch `feat/<slug>` conflicts irreconcilably with `main` mid-milestone (Step 4 rebase surfaces conflicts that touch more than mechanical formatting).
5. Milestone scope change: closing an issue as wrong-premise, or discovered work large enough to rival an existing issue.
6. Consolidation PR review conflict: the human reviewer surfaces a design fork the skill missed. Stop, ask — do not attempt to unwind squash-merged issues.

Everything else has a sensible default: take it, record the decision in the issue/PR and the design journal, keep moving.

## Completion

Two phases:

**Phase A — skill-owned.** Every milestone issue and in-scope follow-up is closed. The integration branch is rebased on `main`. The consolidation PR is open with the finalized design journal as its body. Report to the user: table of issue → PR → merge commit; follow-ups filed with priorities (implemented vs deferred); premise corrections made; post-merge validations run; the consolidation PR URL. **Stop.**

**Phase B — human-owned.** The user reviews the consolidation PR and merges it (or requests changes). When the user confirms merge, close the milestone (`gh api -X PATCH repos/:owner/:repo/milestones/<N> -f state=closed`).

## Failure modes

| Symptom | Resolution |
|---|---|
| Per-issue tests fail on the feature branch and also on `feat/<slug>` | Pre-existing on the integration branch — file/annotate an issue, don't grind the branch. Merge only if the per-issue PR's own tests are green. |
| Tests fail on `feat/<slug>` and also on `main` | Genuinely upstream — file/annotate an issue against `main`; not a milestone blocker. |
| Merge conflict on a per-issue PR | Rebase the feature branch on `feat/<slug>`, re-run tests, push. |
| Integration branch conflicts with `main` mid-milestone | Rebase `feat/<slug>` on `main`, `git push --force-with-lease`, re-run tests. Only this skill writes to `feat/<slug>`, so the force-push is safe. |
| Issue already has an open PR | Yours (this session or a prior run, targeting `feat/<slug>`): adopt and drive it to merge. Someone else's, or targeting `main`: escalate. |
| Issue premise contradicted by evidence | Update or close it with the evidence, comment on the milestone, continue. Escalate only on material scope change. |
| Context compacted mid-issue | Rebuild from per-issue closed comments + `~/.claude/plans/planazo/milestone-<N>-integration.md` + `git log feat/<slug>` + `gh pr list --base feat/<slug>`. |
| `origin/feat/<slug>` exists but points to an unrelated branch | Ask the user before adopting or renaming — never overwrite unknown history. |
| The design-journal file would leak internal names or secrets into the consolidation PR body | Blocker — strip those refs before opening the PR. |

## Anti-patterns

- **Don't implement issues inline on the integration branch.** Even a "one-liner" issue goes through the pipeline — inline fixes skip the critic, the reviewer, and the behaviour-locking test. No commits land directly on `feat/<slug>`: every change arrives via a squash-merged per-issue PR.
- **Don't batch unrelated issues into one per-issue PR.** 1 issue = 1 PR. Bundle only true duplicates, recorded in both issues.
- **Don't merge on red**, or on "probably flaky" without a `feat/<slug>` or `main` comparison.
- **Don't skip post-merge validation because per-issue tests were green.** The integration branch can drift; the merged combination might behave differently.
- **Don't leave bookkeeping for the end.** Per-issue closed comments, issue state, and the design journal are updated per-issue, not in a final sweep — compaction can hit at any time, and the journal *is* the future PR body.
- **Don't merge the consolidation PR yourself.** Preparing it is the skill's job; merging it is the human's. The whole point of the integration-branch flow is that a human reviews one well-documented PR instead of N stacked minimal ones.
- **Don't reconstruct the design journal after the fact.** Squash-commit messages are terse and lose the reasoning; the journal is written as you go so the consolidation PR body is honest, not archaeological.
- **Don't re-ask for permissions this skill grants**, and don't assume ones it excludes.
- **Don't parallelize issues.** Shared working tree; serial merges keep every rebase trivial.
