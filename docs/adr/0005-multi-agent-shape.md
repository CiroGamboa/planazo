# 0005 — Multi-agent shape

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Landed by:** #17 — Instagram Extraction Agent + audit log (`feat(agents)`)
- **Relates to:** [`0002-event-tool-contracts-and-approval-gate.md`](0002-event-tool-contracts-and-approval-gate.md) (`IRREVERSIBLE_TOOLS` scope — `save_event` explicitly outside), [`0003-sqlite-domain-store.md`](0003-sqlite-domain-store.md) (`save_event` public name pinned), [`0006-instagram-extraction-approach.md`](0006-instagram-extraction-approach.md) (source-adapter error taxonomy the Extractor surfaces), [`0007-monitor-scheduling-and-grades.md`](0007-monitor-scheduling-and-grades.md) (monitor's join-by-`run_id`), [`0008-domain-driven-module-layout.md`](0008-domain-driven-module-layout.md) (`extraction/` bounded-context placement — see "Relates to ADR 0008" below), [`0010-extensibility-interfaces.md`](0010-extensibility-interfaces.md) (`EventSource` seam the Extractor consumes).

## Context

M1 landed the Recommender (`agents/event_agent.py::run_once`) driving `save_event` / `search_events` / memory tools against a single Responses-API tool-calling loop. M2 (#16 / [ADR 0006](0006-instagram-extraction-approach.md)) landed the `InstagramSource` adapter that returns a validated `RawPost` payload from a Dockerized scraper. Nothing consumes it: there is no specialist extractor, no delegation brief, no cross-agent audit trail, no `{status, event, needs_approval, notes, error_type}` hand-off shape. This ADR records the multi-agent shape M3 (#17 + companion #18) locks in.

Three concerns drive the shape.

**Trust boundary — AGENTS.md Rule 2.** Scraped captions carry adversarial text; the Extractor is the only module allowed to hold raw scraped text in a prompt, and it must return a validated `Event` (or a typed error state) to the Recommender — never the caption string. This is enforced by module layout (the Recommender's tool registry never imports `sources.instagram.*`) and by the hand-off type (the Extractor returns an `ExtractionResult`, not a `RawPost`).

**Traceability — ADR 0007's join-by-`run_id`.** The monitor reads both agents' JSONL trace lines and joins them on `run_id` before grading. That join only works if the Extractor writes trace lines shaped exactly like the Recommender's — `RunStep` from `monitor/models.py`, with the `agent: Literal["recommender", "extractor"]` discriminator already carrying the extractor arm.

**Composition-root split — ADR 0008.** Each aggregate belongs in its own bounded context. `ExtractionResult` is neither an `Event` nor a `LoopResult` — it is the cross-agent hand-off surface — so it lives in a new `extraction/` context alongside the audit-log writer that populates the trace file.

### Design decisions locked

Every load-bearing decision below names at least one rejected alternative with a reason (AGENTS.md rule 6). The plan file at `~/.claude/plans/planazo/2026-07-28-extraction-agent-17.md` is the primary spec; this section records the decisions for the ADR audit trail.

#### 1. Extractor entrypoint lives at `src/planazo/agents/extractor.py`

Peer of `event_agent.py` (the Recommender), following the composition-root placement ADR 0008 carved out for `agents/`. `DELEGATION_BRIEF` is a module constant in the same file so the "matches verbatim" test imports one name.

*Alternative rejected — `extraction/agent.py` as the composition root, moving `event_agent.py` alongside as `recommendation/agent.py`.* Symmetric but forces a Recommender move that ADR 0008 explicitly deferred; premature. Both composition roots stay under `agents/`; models + audit live under their bounded contexts.

#### 2. `ExtractionResult` lives at `src/planazo/extraction/models.py`

New bounded context `extraction/` matches ADR 0008's per-aggregate layout. The context also houses the audit-log writer and a package `__init__.py` that re-exports the public API.

*Alternative rejected — inline in `agents/extractor.py`.* `LoopResult` (a transient dataclass) sits with the loop; `ExtractionResult` is a Pydantic v2 aggregate that #18 will import from a different composition root, so mixing it into a runtime module violates ADR 0008.

*Alternative rejected — `catalog/models.py`.* `ExtractionResult` wraps an `Event` but is not an event; it is a cross-agent hand-off contract. `catalog/` owns the event catalog, not the delegation surface.

#### 3. `fetch_instagram_post` is an LLM-callable tool, closured over an `InstagramSource` instance

Exported by a factory in `src/planazo/sources/instagram/tools.py`. Mirrors `catalog/tools.py`'s adapter-method + flat-scalar LLM tool wrapper split, and `memory/api.py::build_memory_tools`'s dependency-injection-by-closure discipline. Signature (LLM-visible): `fetch_instagram_post(url: str) -> dict[str, object]`. Return: a JSON-serializable dict — the `RawPost.model_dump(mode="json")` on success, or the source adapter's typed error dict (`{"error_type": ..., "message": ..., "url": ...}`) unchanged. Factory returns `(schema, callable)`; the schema is derived by `tools.schema.schema_for` from the callable's signature.

*Alternative rejected — Extractor calls the adapter directly in Python, before the LLM turn.* Cleaner in one axis (no wrapper file) but drops the LLM's visibility into "I need to fetch this post" as a tool call, which the monitor uses to reconstruct the delegation trace. Also breaks the ticket AC binding `TOOL_REGISTRY = {fetch_instagram_post, save_event}`.

*Alternative rejected — wrapper inside `agents/extractor.py`.* Puts source-adapter integration in the wrong context; other future consumers (a batch backfill CLI, for instance) would have to reach into `agents/`.

#### 4. `save_event` is not gated in the Extractor's run

ADR 0002 pins `IRREVERSIBLE_TOOLS = {"confirm_and_create_calendar_event"}`. `save_event` writes to a private SQLite row with a UNIQUE `source_url` (already handled by the `duplicate_event` typed branch); no third-party effect, no email, no calendar visibility. The Extractor's `run_loop` call passes `gate=None`. This is the correct read of ADR 0002 rule 3 ("effect visible to a third party" = irreversible; a private row is not).

*Alternative rejected — Extractor takes an `ApprovalGate` from its delegator.* Adds a synchronous approval prompt inside the delegated Extractor, defeats the point of delegation (the Recommender is the user-facing surface; the Extractor is the specialist doing the work).

*Alternative rejected — auto-approving `NoApprovalGate`.* Extra code for zero behavioural difference vs. `gate=None`.

#### 5. Terminal state signalled by an explicit LLM tool call

The Extractor's tool registry is three tools: `fetch_instagram_post`, `save_event`, and new `report_extraction_status(status, error_type, notes)`. The LLM ends a run by calling exactly one of `save_event` (success) or `report_extraction_status` (unhappy). `extract_once` inspects the trace's tool calls to determine the terminal state — no JSON-parsing of `LoopResult.answer`. The delegation brief instructs the LLM accordingly via a `#### Terminal calls` sub-block in `MVP-ARCHITECTURE.md §Delegation brief — Extractor`.

*Alternative rejected — parse `LoopResult.answer` as JSON.* AGENTS.md rule 4 fragility: any malformed answer (extra whitespace, backticks, natural-language wrapper) becomes an untyped error. Explicit tool call is a code-shape guarantee.

*Alternative rejected — `text_format` on the final turn.* Requires wiring `text_format` through `run_loop`, and Responses-API `text_format` on a tool-calling turn conflicts with the tool-call output shape; not worth the seam.

#### 6. Multimodal image reaches the LLM via a new `run_loop` `on_tool_output` hook

Signature: `on_tool_output: Callable[[StepRecord], list[dict[str, Any]] | None] | None = None`. When set, `run_loop` calls it with the just-dispatched `StepRecord` and, if the callable returns a non-empty list, appends those Responses-API message dicts to the transcript *after* that call's `function_call_output` — before the next LLM turn. `None` return is a no-op; the loop's behaviour is byte-identical to today's when the hook is omitted. `run_loop` still holds no domain knowledge (the hook decides what to inject).

*Alternative rejected — tool wrapper returns a base64 image string and `run_loop` interprets a special sentinel key.* Couples `run_loop` to a tool return contract that only images need; `on_tool_output` keeps the semantics on the caller side.

*Alternative rejected — Extractor bypasses `run_loop` and writes its own small loop.* Loses the shared trace / step-count / gate machinery; violates rule "prefer editing" and duplicates a working piece of the runtime.

*Alternative rejected — image URL is included in the `function_call_output` string; LLM "reads" it as text.* Responses API only surfaces images via `input_image` content parts; a URL in a `function_call_output` string reaches the LLM as text, not vision.

#### 7. "One image per call" enforced by the hook picking exactly one visual asset

Selection rule, in order: (a) first `MediaAsset` with `kind == "image"`; (b) else first `MediaAsset` with `kind == "thumbnail"`; (c) else no visual asset — the hook appends only an `input_text` note ("no visual asset available for this post") and the LLM falls back to caption-only extraction. Deterministic; matches how M2's adapter maps `GraphImage` (image), `GraphSidecar` (image nodes first), and `GraphVideo` (video + thumbnail).

*Alternative rejected — attach every image asset.* Blows the cost envelope (multiple STRONG-tier image calls per run) and violates the delegation-brief bullet "one image per call".

#### 8. Audit log schema reuses `RunStep` from `monitor/models.py`

`RunStep.agent: Literal["recommender", "extractor"]` already carries the extractor arm. `monitor/service.py` reads a joined stream from `data/runs/*.jsonl` (Recommender) and `var/extraction_runs.jsonl` (Extractor). One line per turn: `tool_dispatch` for each tool call, one `completion` line at the end with the run's terminal `stopped` reason. Extractor writes to a single append-only file `var/extraction_runs.jsonl` (contrast: Recommender writes per-run files under `data/runs/`); MVP-ARCH §9 and the monitor's join-by-`run_id` seam depend on this shape.

*Alternative rejected — separate `ExtractionRunStep` schema.* Duplicates 12 fields for one string change (agent literal already covers it). Monitor's join code would need a discriminated union with no upside.

*Alternative rejected — per-run file `var/extraction_runs/{run_id}.jsonl`.* Fragments the log across N files when the monitor's design goal is "one append-only extraction log the judge can tail".

#### 9. `extraction_runs_index` populated at run start

Every `extract_once` call inserts one `ExtractionRunIndexEntry(run_id, user_id=delegator_user_id, url, started_at)` row via `record_extraction_run` (ADR 0003, `catalog/repository.py`) before the LLM turns begin. This is the SQLite-side pointer into the JSONL log — MVP-ARCH §7's contract. Tests seed a `users` row before invoking `extract_once` (identity/repository already exposes `insert_user`); production callers pass a real `users.id` from the bot session.

*Alternative rejected — index only on success.* Loses the trace when the LLM crashes mid-run; the monitor's join-by-`run_id` would silently drop failed runs.

#### 10. Extraction result cardinality: one post → at most one `Event`

The LLM is instructed (in the delegation brief) to pick the primary event a post announces; carousels announcing multiple distinct events return `report_extraction_status(status="needs_clarification", error_type="multiple_events_in_post", ...)`. Multi-event support is filed as a follow-up.

*Alternative rejected — return `list[Event]`.* Complicates the Recommender's downstream consumer (which currently branches on a single `Event | None`) for a case the MVP does not yet need to handle.

#### 11. Error taxonomy — `ExtractionResult.error_type` typed literal

Branches: `unsupported_source`, `rate_limited`, `auth_failed`, `not_found`, `unsupported_media` (verbatim from `InstagramSource.fetch_post` — ADR 0006 taxonomy), `low_confidence_extraction`, `missing_date`, `location_out_of_metro`, `multiple_events_in_post`, `ambiguous_content`, `no_visual_asset`, `save_event_failed`. Enforced by `Literal[...]` on `ExtractionResult.error_type`. `status == "ok"` requires `error_type is None` and `event is not None`; `status in {"error", "needs_clarification"}` requires `error_type is not None` and `event is None`. Enforced by a Pydantic `model_validator(mode="after")`.

*Alternative rejected — a free-form `str` error code.* Loses type-checking at the boundary; a typo silently produces an untriaged branch. AGENTS.md rule 4 requires typed error branches.

#### 12. `DELEGATION_BRIEF` byte-verbatim with MVP-ARCH §Delegation brief

The constant holds the five bullets that follow the pre-amble line plus a `#### Terminal calls` sub-block this plan adds to MVP-ARCH describing the `save_event` / `report_extraction_status` terminal contract. MVP-ARCH §Delegation brief — Extractor is edited in the same PR to include the `#### Terminal calls` sub-block (heading level 4, correctly nested under the level-3 section header) — so "verbatim" has one authoritative source. The block is delimited by two HTML comment anchors, `<!-- extraction-delegation-brief:start -->` and `<!-- extraction-delegation-brief:end -->`, bracketing the five bullets + `#### Terminal calls` sub-block. Anchor-based extraction is deliberately used instead of heading-rank parsing so future heading additions to MVP-ARCH cannot silently truncate what the test locks.

*Alternative rejected — inline the brief text in `extractor.py` alone.* Two sources of truth (doc + code) drift; MVP-ARCH is the reference document the team reads and the code constant must match it byte-for-byte.

*Alternative rejected — parse by heading rank.* Any future heading addition or nesting change silently truncates what the test locks; the anchors make the contract explicit at the doc's own layer.

#### 13. `ExtractionResult.notes` uses a short cap + adversarial redaction test

`notes: str = Field(default="", max_length=200)`. AGENTS.md Rule 2 ("Extractor is the only module that holds raw scraped text and returns the parsed `Event`, never the caption string") is enforced by code shape — 200 chars is smaller than any real caption's usable length, so a paraphrase can pass through but wholesale quoting cannot. Stage 3 adds an adversarial test: fake `InstagramSource` returns a `RawPost` with a caption; fake LLM's `report_extraction_status` call passes that caption text as the `notes` argument; the test asserts the resulting `ExtractionResult` either fails validation (over max_length) OR — if the LLM adversarially truncated — `notes` does not contain any substring of the caption longer than 40 chars.

*Alternative rejected — no cap, rely on prompt discipline alone.* Prompt discipline is unenforceable; a length cap is a code-shape guarantee (rule 2's enforcement site row in MVP-ARCH is "code shape, not prompt discipline").

*Alternative rejected — no `notes` field at all.* Loses operator-facing diagnostics on unhappy branches; the audit log's `final_answer` is verbose, `notes` is the short-form headline the Recommender surfaces to the user.

### Relates to ADR 0008

ADR 0008's target-tree section reserved a `discovery/` placeholder for M2/M3 domain entities. That placeholder is now fully retired: M2 ([ADR 0006](0006-instagram-extraction-approach.md)) claimed `sources/`, and this ADR claims `extraction/`. There is no package named `discovery/` in the tree post-M3; a future reader of ADR 0008 chasing the `discovery/` name lands here (for `extraction/`) and at ADR 0006 (for `sources/`) for the settlement.

## Decision

Planazo adopts a two-agent shape: a **Recommender** (`agents/event_agent.py`, CHEAP tier) that owns the user conversation, and a specialist **Extractor** (`agents/extractor.py`, STRONG tier) that owns Instagram-post → `Event` extraction. The Recommender delegates to the Extractor via `dispatch_extraction(url, user_id) -> ExtractionResult`; the Extractor's front door is `extract_once(url, delegator_user_id) -> ExtractionResult` running on `STRONG` with `max_steps=4` + `max_output_tokens=2000`, driven by a `DELEGATION_BRIEF` byte-verbatim with MVP-ARCH §Delegation brief, calling `fetch_instagram_post` (M2 adapter closured over the LLM tool boundary), `save_event` (M3 catalog tool), and `report_extraction_status` (terminal unhappy branch) via its own tool registry. The hand-off is `ExtractionResult(status, event, needs_approval=False, notes, error_type)` with the (status ↔ error_type ↔ event) invariant enforced by a Pydantic `model_validator`. Every Extractor run appends one `RunStep(agent="extractor", ...)` JSONL line per turn to `var/extraction_runs.jsonl` and one `ExtractionRunIndexEntry` row to `extraction_runs_index`; the monitor joins those trace lines to the Recommender's `data/runs/*.jsonl` on `run_id`. `save_event` runs without an `ApprovalGate` in the Extractor (ADR 0002); the trust boundary is enforced by module layout (`sources.instagram` is imported only by the Extractor's tool registry, never by the Recommender).

## Consequences

### Positive

- **The trust boundary is a code-shape guarantee.** The Recommender's tool registry does not import `sources.instagram.*`; the Extractor's registry does. `ExtractionResult.notes`'s 200-char cap is smaller than any real caption's usable length, so a compromised Extractor cannot smuggle raw caption bytes to the Recommender through the hand-off.
- **The monitor sees both sides of the delegation.** Joining `data/runs/*.jsonl` and `var/extraction_runs.jsonl` on `run_id` reconstructs the full delegation trace; a swallowed error or a mid-run race becomes visible without either agent complaining.
- **Terminal state is a code-shape guarantee.** The LLM ends a run by calling `save_event` (success) or `report_extraction_status` (unhappy); `extract_once` inspects the trace's tool calls rather than parsing `LoopResult.answer` as JSON. No malformed-answer fragility.
- **The delegation surface is one Pydantic aggregate.** `ExtractionResult` is what the Recommender's `dispatch_extraction` tool imports — one contract, one validation site, one bounded context to bump when the shape changes.
- **`run_loop` stays domain-agnostic.** The `on_tool_output` hook is a generic seam; the Extractor supplies the hook and the multimodal-image logic. Future non-image cross-turn injections (a hypothetical citation-fetcher tool that appends its retrieved document as a message) reuse the same seam without another `run_loop` change.

### Negative / accepted trade-offs

- **`notes` short cap may truncate legitimate operator explanations.** `max_length=200` is deliberately tight to enforce AGENTS.md Rule 2 by code shape. The Extractor truncates (not raises) when a `report_extraction_status` unhappy branch produces an explanation longer than 200 chars; the audit log's `RunStep.final_answer` carries the LLM's full text and is where a debugger looks for the untruncated explanation.
- **`extraction_runs.jsonl` grows unbounded.** MVP accepts append-only; monitor already handles single-file scan. Parity with `data/runs/*.jsonl` growth (also unbounded); rotation is a follow-up if either file becomes a real problem.
- **`DELEGATION_BRIEF` verbatim test is deliberately brittle.** Any edit to the anchor-bracketed block in MVP-ARCH without a matching edit to `DELEGATION_BRIEF` fails the test — this is the point (ticket AC binds them). Anchor-based extraction (not heading-rank) is used so future heading additions or nesting changes cannot silently drop text from what the test locks.
- **Instagram CDN URL expiry.** `MediaAsset.url` may 404 by the time the multimodal LLM fetches it; ADR 0006 names `refresh(shortcode)` as a follow-up. MVP acceptance is "extract quickly after fetch".
- **Multi-event carousels return `needs_clarification`.** A promoter announcing three shows in one post cannot be split into three `Event` rows today; the LLM returns `multiple_events_in_post`. The follow-up "N events per post" is filed as a milestone-journal note.

### Follow-ups

- **Recommender-side `dispatch_extraction`.** Companion ticket #18 in the same milestone. Imports `extract_once` + `ExtractionResult` from `extraction/`; adds the tool to the Recommender's registry.
- **Recommender executor extension.** Accept `user_id` + `SearchIntent` (M4, #19).
- **Ranker.** M4, #20.
- **Multi-event carousel support.** N-events-per-post filed as a follow-up when a real promoter's carousel forces the issue.
- **Reels / video multimodal parsing.** First pass targets static posts + carousel first-image + reel/video thumbnail; video-frame extraction is a follow-up.
- **`extraction_runs.jsonl` rotation.** Only if the file ever becomes a monitor-scan problem.
- **Meetup / Eventbrite adapters.** ADR 0011, conditional on either shipping past POC.
