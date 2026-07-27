# CLAUDE.md

Read `AGENTS.md` for all project instructions — it is the single source of truth.

## Claude-Specific Notes

- Claude Code reads this file automatically on session start.
- All rules, commands, architecture, and conventions are in `AGENTS.md`.
- Skills are user-invocable only via `/<skill-name>`. The canonical list:
  - `/writing-development-tickets` — scope and file a well-formed GitHub issue.
  - `/executing-development-tickets <N>` — drive one issue end-to-end: architect → critic → stage implementers → reviewer → PR. Accepts `base_branch=<ref>` for milestone use.
  - `/implement-milestone <N>` — drive a whole milestone: integration branch + per-issue pipeline + running design journal + single consolidation PR from `feat/<slug>` to `main`.
- Agents in `.claude/agents/` are dispatched by the executing skill; they are not called directly by the user.
- Plans live at `~/.claude/plans/planazo/`, not in the repo. They flow into PR bodies at `gh pr create` time.
