# S5 individual submission — run monitor

**Student:** Daniel  
**Contribution:** Issue #4 — independent LLM-as-judge run monitor  
**Commit links:** Replace these with the GitHub links after pushing `feat/run-monitor`:

- Monitor implementation: `COMMIT_LINK_1`
- Integration with the memory work: `COMMIT_LINK_2`
- Missing-key UX fix: `COMMIT_LINK_3`

## What I implemented

I implemented an out-of-band monitor for Planazo's agent runs. Its purpose is to review completed recommender and extractor runs against the project rules, rather than letting the user-facing agents evaluate themselves while they are responding.

Each agent run writes a structured JSONL trace under `agent/data/runs/`. The trace records tool dispatches and a completion event, including the final answer and stop reason. A separate `planazo-monitor` command reads those traces, sends each trace to a stronger LLM acting as a judge, validates the returned result with Pydantic models, and produces both a Markdown report and a JSONL report. Reports group findings by day and run, so the team can investigate a problematic interaction without reading unrelated logs.

The monitor classifies three distinct questions with named categorical fields:

| Question | Possible outcomes |
| --- | --- |
| Did the run follow the rulebook? | `strictly_adheres`, `minor_violation`, `serious_violation` |
| Did the run stay safe around untrusted content? | `safe`, `near_miss`, `obeyed` |
| Is human review needed? | `true` or `false` |

For every result other than the clean/safe outcome, the judge must provide a short rationale. This avoids an ambiguous single score and makes the report actionable. The CLI also has a `--dry-run` mode that evaluates deterministic seed sessions, including a clean run, a rule violation, and a prompt-injection near miss.

## How this applies the course ideas

The central idea is **LLM-as-judge**. The monitor uses a stronger model with a fixed judging prompt and treats the agent trace as quoted, untrusted data. The trace is not allowed to change the judging instructions. This directly addresses the trust boundary between system instructions and user/event content.

The design also uses **boundary validation**: the judge output is parsed into Pydantic models with only the allowed categories. Invalid output cannot silently become a valid safety decision. In addition, the monitor is deliberately **out of band**. It does not add latency to a user interaction, does not decide the normal agent's answer, and can inspect completed runs independently. Separating the executor/recommender from the judge makes the evaluation less self-serving and makes post-hoc review possible.

The monitor integrates with the project's memory work without reading or exposing private memory directly. It reviews the observable run trace and reports the evaluation result. This keeps personal memory and monitoring concerns separated.

## Testing and evidence

I verified the integrated repository with the following checks:

- `uv run pytest`: **155 passed, 2 deselected**
- `uv run ruff check .`: passed
- `uv run ruff format --check .`: passed
- `uv run mypy src`: passed
- `planazo-monitor --dry-run`: verifies the CLI's deterministic monitor path; when no API key is configured, it now gives a clear setup message instead of a traceback.

The integrated memory demonstrations were also run successfully and created local evidence showing that a private preference for one user is not returned to another user, while a shared note is available to the other user:

- `agent/docs/evidence/private-memory.md`
- `agent/docs/evidence/shared-memory.md`

## Final evidence to generate before submission

Two live-provider artifacts are still required to make the submission fully evidenced. They cannot be truthfully claimed until an `OPENCODE_API_KEY` is configured and the commands complete:

1. Run `uv run python scripts/demo/untrusted_content.py` from `agent/` and retain the generated `agent/docs/evidence/untrusted-content.md`. This demonstrates that the planted event comment is treated as untrusted content rather than instructions.
2. Run `uv run planazo-monitor --dry-run` (and, after real agent runs exist, `uv run planazo-monitor --since 24h`) and retain the generated monitor report under `agent/data/monitor/`. The report should include the judge rationale for any non-clean result.
3. Push this branch before submitting so the three commit links above are accessible to the teacher.

## Scope note

My individual monitor slice is implemented, integrated with the current memory work, and verified by the local test suite. The group repository still needs the second course-required agent/extractor work to be completed and integrated before the **whole team homework** can be represented as complete. This document describes my individual contribution only.
