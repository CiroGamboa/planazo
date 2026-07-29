# ADR 0017: Instagram demo narrative logs

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** cirogam22
- **Landed by:** M3.7 T3
- **Relates to:** [`0005-multi-agent-shape.md`](0005-multi-agent-shape.md) (the `on_step` observer seam this ADR reuses), [`0011-scheduled-ingestion.md`](0011-scheduled-ingestion.md) (the `--tick` output shape that this ADR preserves unchanged), [`0013-extractor-side-frame-extraction.md`](0013-extractor-side-frame-extraction.md) (reel frames still live inside the multimodal hook — not a distinct tool the narrative logger observes), [`0014-instagram-discovery-backends.md`](0014-instagram-discovery-backends.md) (the backend split the demo README documents), [`0015-storage-migrations-and-observability.md`](0015-storage-migrations-and-observability.md) (the DB-inside audit surface the narrative log complements — never replaces).

## Context

Three forces converged going into M3.7 T3.

**The single-URL demo shipped in M3.5 has no live narrative.** `planazo-scheduler --once <URL>` runs the full extraction pipeline (fetch → multimodal LLM turn → `save_event` or `report_extraction_status`) and emits exactly one JSON line at the end — `SchedulerRunRecord.model_dump_json()`. Between "URL in" and "JSON out" there is silence: no `logger.info` calls across `extract_once` or the loop, only two `logger.warning` lines on failure paths. An operator running the demo in front of a live audience sees a terminal that appears to hang for 5-20 seconds and then prints a compact JSON blob. The extraction itself is happening — the trace is written to `var/extraction_runs.jsonl` — but the JSONL sidecar is a post-hoc audit surface, not a real-time signal.

**Rule 2 forbids captions or LLM output on stdout.** Any decorative narrative layer risks bleeding caption bytes (`RawPost.caption`), LLM-produced text (`LLMDecision.rationale`, `LoopResult.answer`, `Event.title`, `Event.description`), or free-form arguments (`report_extraction_status(notes=...)`) into a terminal transcript that will be pasted into PRs, Slack, and demo screenshots. The DB-inside audit surface (`agent_runs` + `llm_decisions`) is engineered to hold full sanitized text inside the trust boundary; a demo transcript is outside it. The narrative layer must be strictly structural — timestamps, shortcodes, integer counts, Literal-valued fields — with no free-form text interpolation.

**The cron-driven `--tick` output must stay stable.** The existing `--tick` code path lands one JSON line per URL in `var/scheduler_runs.jsonl` and prints nothing to stdout during a tick. Cron log-rotation and wrapper alerting scripts depend on this shape. Any narrative addition that fires unconditionally would either break the cron log-rotation shape or force operators to re-verify every wrapper script. The demo shape and the cron shape must diverge cleanly.

Doing all three (real-time signal, Rule 2 compliance, cron-shape preservation) in one ticket means the narrative log lands as a strictly opt-in, strictly-structural, stdout-only observer wired only into the demo command.

## Decision

Planazo lands a `NarrativeLogger` class in `src/planazo/sources/instagram/narrative.py`, wired into `planazo-scheduler --once <URL> --verbose` via `extract_once`'s new `on_step` + `on_complete` observer seams. The four load-bearing choices below encode both the shape and the discipline.

### 1. Narrative log is opt-in via `--verbose`, not default

The scheduler CLI grows a `--verbose` flag (default `False`). Cron ticks (`--tick`) never construct a `NarrativeLogger`; the JSONL-only output shape stays byte-identical to M3.5. The demo command (`--once <URL> --verbose`) wires the narrative logger through the shared `_run_once` composition root.

**Rejected alternatives:**

- **Emit the narrative log by default on every `--once` invocation.** Rejected: `--once` is documented as a diagnostic single-URL path a wrapper script or an operator could invoke non-interactively (e.g. from a CI job that runs `--once` to smoke-test a new URL against the current codebase). Changing its stdout shape without an opt-in would break any script that currently pipes `--once` output into a downstream tool. Making the narrative opt-in preserves backward compatibility while giving the demo the signal it needs.
- **Emit the narrative log on every `--once` and every `--tick`.** Rejected for the same reason as above, doubled: cron log-rotation depends on the `--tick` shape, and a wrapper alerting script that greps for `"error_type"` in the JSONL would see false positives from a narrative line quoting a Literal `error_type` field. The narrative surface is a demo affordance; the cron surface is an operator contract. Conflating them creates a false economy.
- **Ship a separate `planazo-demo` CLI that wraps `planazo-scheduler --once`.** Rejected: `--verbose` is the tightest possible seam. A new CLI adds an entry point in `pyproject.toml`, a new argparse surface, a new set of tests. `--verbose` fits inside the existing argparse group without a subcommand split.

### 2. Narrative log is stdout-only, no persistence

The narrative logger writes to `sys.stdout` (or an injected `TextIO` for tests) and nowhere else. There is no file sidecar, no rotation, no rollover. The JSONL sidecar under `var/extraction_runs.jsonl` remains the durable audit surface and is written on every `--once` and every `--tick` invocation regardless of `--verbose`.

**Rejected alternatives:**

- **Write the narrative to a companion file (`var/demo_narrative_<run_id>.txt`).** Rejected: two writer surfaces for the same run means two divergence risks — a narrative that drifts from the JSONL, an operator confusion about which file is canonical. The JSONL sidecar already carries the structured trace (one `RunStep` per tool dispatch, one completion line per run); anyone who wants a human-readable rendering of a historical run can grep the JSONL and format it after the fact. Adding a second writer surface for a decorative concern violates AGENTS.md Rule 8.
- **Log the narrative through the standard `logging` module at INFO level.** Rejected: `logging.INFO` interacts with the rest of the codebase's handlers (a downstream configuration could redirect INFO to `var/agent.log`, mixing narrative decorations with operational log lines). The narrative is a display concern targeted at one specific terminal transcript — `print()` to a specific stream is the right vocabulary.

### 3. Log lines follow `[HH:MM:SS] verb + structural subject` shape, strictly excluding LLM output

Every line begins with `[HH:MM:SS]` in UTC (matching the JSONL sidecar's timestamp discipline) followed by a verb-first structural description. Interpolation is restricted to URLs, shortcodes, integer counts (media count, event index, step count), float values with a fixed decimal shape (confidence to two places), and Literal-typed fields (`status`, `error_type`, `category`, `stopped`). LLM-produced strings — `Event.title`, `Event.description`, `RawPost.caption`, `LoopResult.answer`, `LLMDecision.rationale`, `report_extraction_status(notes=...)` — must NEVER be interpolated into the narrative stream.

Concretely the per-phase outputs are:

```
[HH:MM:SS] Fetching post <shortcode> from Instagram...
[HH:MM:SS] Fetched post - <n> media asset(s)
[HH:MM:SS] Saved event at index <i> - category=<literal>, confidence=<float>
[HH:MM:SS] Reported <status>: <error_type>
[HH:MM:SS] Loop terminated: stopped=<literal>, steps=<n>
```

Failure branches (`fetch_instagram_post` returning `error_type`, `save_event` returning `duplicate_event` / `invalid_event_data`, etc.) emit their own structural `error_type=<literal>` line — the `message` field on any typed error dict is NOT read (an upstream API body could be echoed inside `message`).

**Rejected alternatives:**

- **Echo `event.title` on the "saved event" line for demo readability.** Rejected: `Event.title` is LLM-produced. Even after `format_stored_text` sanitization the string could paraphrase a caption verbatim if the LLM chose to. Adding it to stdout is a Rule 2 violation — the trust boundary must be preserved on the way out of the process, not just at the DB write. The demo audience sees `category=music` and `confidence=0.87` alongside the URL/shortcode; anyone wanting the title can `sqlite3 var/planazo.db "SELECT title FROM events WHERE id = <db_id>"` after the fact.
- **Echo `report_extraction_status(notes=...)` for the diagnostic on failure.** Rejected: `notes` is the free-form LLM diagnostic surface, capped at 200 chars but not otherwise structural. It's exactly the surface a hostile caption tries to smuggle prompt-injection content through. The narrative line carries `status: error_type` — the Literal-valued shape — and operators reach `notes` through the DB-inside `llm_decisions.rationale` column.
- **Local wall-clock timestamps in the operator's `TZ` rather than UTC.** Rejected: the JSONL sidecar timestamps are UTC. Two timestamp bases in the same demo transcript would be confusing — the audience can correlate the narrative line with the `RunStep.recorded_at` field only if both use the same zone.

### 4. The `on_step` observer seam is the wiring, not a change to `extract_once`'s internals

`extract_once` grows two keyword-only observer parameters — `on_step: Callable[[StepRecord], None] | None` and `on_complete: Callable[[LoopResult], None] | None`, both defaulting to `None`. The narrative logger's `__call__` and `.complete` methods bind directly to these. The internal composition (JSONL sidecar via `ExtractionRunLogger`, best-effort `AgentRunLogger` + `LLMDecisionLogger` writes at the end of the loop) is untouched — the narrative logger is a third observer layered on top, never a replacement.

The scheduler CLI is the composition root that wires the narrative: `_run_once` constructs a `NarrativeLogger(url=url)`, calls `.start()` before the extraction, wraps the `ExtractorCallable` in a closure that passes `on_step=narrative` + `on_complete=narrative.complete` to `extract_once`, and lets the JSONL sidecar continue its own path.

**Rejected alternatives:**

- **Add `logger.info` calls inside `extract_once` and configure a stdout handler for `INFO` level when `--verbose` is set.** Rejected: `logger.info` couples the narrative surface to Python's `logging` module — every downstream configuration change (a new handler, a filter, a formatter) risks changing the demo output. The observer seam is a first-class dependency-injected surface with a stable `Callable[[StepRecord], None]` contract; `logging` is a global side-effect channel.
- **Fold the narrative logic into `ExtractionRunLogger`.** Rejected: `ExtractionRunLogger` is the JSONL sidecar's writer, always-on, always-recording. Layering a display concern on top would mean either an `if verbose: print(...)` branch inside the sidecar writer (mixing concerns) or a subclass that both writes JSONL and prints stdout (concerns fused). The observer seam is a clean separation — sidecar always fires, narrative fires only when a caller opts in.
- **Give the narrative logger a subscription API where it registers itself with `run_loop` from outside `extract_once`.** Rejected: `run_loop`'s `on_step` seam is already parameterised — the observer sits at the composition root, one layer above the loop. Adding a registration surface (a global observer bus, a module-level list) would fragment the responsibility for wiring observers between `extract_once` and its callers. The current keyword-only params keep the wiring visible at the composition root.

## Consequences

### Positive

- Live demos of `planazo-scheduler --once <URL> --verbose` produce a step-by-step transcript that a human audience can follow in real time. The JSONL sidecar still exists for post-hoc analysis.
- The cron path (`--tick`) is byte-identical to M3.5 — no operator script needs to be updated.
- Rule 2 discipline is now enforced at two layers (DB-inside sanitization + stdout-outside structural-only) with an ADR-level policy statement rejecting any future PR that adds LLM output to the narrative stream.
- The `on_step` / `on_complete` seams on `extract_once` are now a documented extension surface — a future ticket that wants to add a WebSocket-based live monitor, a Slack-formatted narrative, or a metrics counter can bind to the same shape without further changes to `extract_once`.

### Negative / accepted trade-offs

- The narrative logger does not describe reel-frame extraction as a distinct phase. `extract_reel_frames` is called inside the multimodal `on_tool_output` hook, not as a first-class tool the loop's `on_step` fires on. A live demo of a reel URL will see the "Fetched post — 1 media asset(s)" line and then a pause (during frame extraction + the LLM turn) before the "Saved event" line. Operators can watch `var/extraction_runs.jsonl` in a second terminal for the ffmpeg-level detail. A follow-up ticket could lift frame extraction into a first-class loop tool if the demo need becomes acute.
- The narrative logger's output is not tested against a live `extract_once` run in CI — the tests fabricate `StepRecord` and `LoopResult` values directly and assert on captured `StringIO` output. Coverage of the integration seam (does `extract_once` actually pass the observer through to `run_loop.on_step`?) rides on the existing extractor tests plus the manual E2E smoke documented in `docs/evidence/m37-instagram-demo.md`.
- `--verbose` is a boolean flag; there is no "medium verbosity" for operators who want the setup + completion lines but not the per-step lines. If a future demo grows a preference for finer control, the flag can be widened to a Literal-valued `--log-level` — the current shape is the MVP surface.

### Follow-ups

- Land a `--verbose` mode for `planazo-agent` (the Recommender CLI) mirroring the same seam — `event_agent.run_once` would gain the same `on_step` observer contract.
- Lift `extract_reel_frames` into a first-class loop tool if the demo need grows, so the narrative line "Extracting frames from reel..." fires naturally through `on_step` rather than needing a side-channel print inside the multimodal hook.
- Consider a colored-output variant using `rich` / `click.style` for terminal demos — kept out of scope for MVP because CI transcripts would then carry ANSI escapes that grep tooling has to filter.
