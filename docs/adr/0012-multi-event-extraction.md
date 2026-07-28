# 0012 — Multi-event extraction: 0..N events per post

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** cirogam22
- **Relates to:** [`0005-multi-agent-shape.md`](0005-multi-agent-shape.md) (supersedes §Decision 10; partially supersedes §Decision 11's invariant clause), [`0003-sqlite-domain-store.md`](0003-sqlite-domain-store.md) (`save_event` tool contract).

## Context

ADR 0005 §Decision 10 locked the extraction result cardinality at one post → at most one `Event`. The LLM was instructed to pick the primary event a post announces, and carousels announcing multiple distinct events returned `report_extraction_status(status="needs_clarification", error_type="multiple_events_in_post", ...)`. M3 shipped that shape.

M3.5's scheduler targets curator accounts (`@bcn.agenda`, `@curated.agenda`, ...) that routinely post carousels announcing multiple distinct events in one post. Under the singular shape, every such carousel returns `needs_clarification` and the scheduler burns LLM budget for zero yield. #64 is the compatibility-surface change that lifts the one-Event-per-post cardinality.

The load-bearing forks:

- **Cardinality is now 0..N events per post.** `ExtractionResult.events: list[Event] = Field(default_factory=list)` replaces `ExtractionResult.event: Event | None`. `status == "ok"` ⇔ `len(events) >= 1`; `status in {"error", "needs_clarification"}` ⇔ `events == []`. Enforced by a Pydantic `model_validator(mode="after")`.
- **`event_index_in_post` is LLM-visible on `save_event`.** New optional parameter `event_index_in_post: int = 0` (last positional). Single-event callers unchanged; carousels supply `0, 1, 2, ...`. The composite `(source_url, event_index_in_post)` is the natural key. `event_index_in_post < 0` returns `invalid_event_data`; a second call with the same `(source_url, event_index_in_post)` returns `duplicate_event` with the existing row's id.
- **`events` table gains a column and swaps its unique key.** `event_index_in_post INTEGER NOT NULL DEFAULT 0` column; `UNIQUE(source_url)` becomes `UNIQUE(source_url, event_index_in_post)`.
- **New primitive `events_exist_for_source_url(conn, url) -> list[int]`.** Returns the sorted list of already-persisted `event_index_in_post` values for `url`. Empty list ⇒ URL has never been persisted; non-empty ⇒ at least one slot filled. The scheduler skips URLs where this returns non-empty.

## Decision

Planazo lifts the extraction cardinality to 0..N events per post. `ExtractionResult` becomes `events: list[Event]` (invariant `status == "ok" ⇔ len(events) >= 1`), enforced by a Pydantic `model_validator(mode="after")`. `save_event` gains an LLM-visible `event_index_in_post: int = 0` parameter and the `events` table gains a matching column with a composite `UNIQUE(source_url, event_index_in_post)` constraint; a second call with the same `(source_url, event_index_in_post)` returns `duplicate_event` with the existing row's id. A new `catalog/repository.py::events_exist_for_source_url(conn, url) -> list[int]` primitive returns the sorted slot indices already persisted for a URL. This ADR supersedes ADR 0005 §Decision 10; ADR 0005 §Decision 11's invariant clause is partially superseded — the new invariant is `status == 'ok' ⇔ len(events) >= 1`; the error-taxonomy body of §Decision 11 is unchanged.

### Alternatives rejected

- **Auto-derive `event_index_in_post` from the trace's `save_event` record count in `_build_result`.** Hides the concept from the LLM but loses duplicate detection: two identical `save_event` calls for the same event would each get their own auto-assigned slot and both persist, corrupting the row set. Making the index a real parameter lets the composite UNIQUE key fire `duplicate_event` on the LLM's retry — the model sees the error branch and reacts.
- **Transitional `event` property alias (`@property def event(self) -> Event | None: return self.events[0] if self.events else None`).** Rejected: AGENTS.md rule 8 forbids `_legacy_*` shims. Every reader is updated in the same PR.
- **`status="partial_ok"` for the mixed success + failure case.** Three-way status is more surface than the milestone needs. Partial success is `status="ok"` with the successful subset of events; failed `save_event` records are noted in `notes` (with the redacted `[error_type: <token>]` construction to close the Rule 2 leak channel). This preserves M3's "get any event you can" bias while adding the multi-event surface.

## Consequences

### Positive

- **The scheduler yields real events on curator carousels.** A three-event carousel becomes three `Event` rows in one run rather than a `needs_clarification` branch.
- **Idempotency is a code-shape guarantee.** The composite `UNIQUE(source_url, event_index_in_post)` means the LLM's retry after a partial success finds the earlier slots taken and only saves the new ones — no double-persistence, no drift.
- **Single-event callers are unchanged at the tool-contract level.** `save_event`'s new parameter defaults to `0`; a caller that never mentions `event_index_in_post` gets the same behaviour as before.
- **The trust boundary is preserved.** `ExtractionResult.notes` still 200-char capped; multi-event `_build_result` uses the same redacted-error-type construction on the mixed-success-plus-failure branch, closing (rather than widening) the pre-existing Rule 2 leak channel in `_build_result`'s save-failure path.

### Negative / accepted trade-offs

- **`schema_v1.sql` is rewritten in place.** `db.py::connect()`'s `CREATE TABLE IF NOT EXISTS` silently no-ops the DDL change against a pre-existing dev DB. Devs with a stale `var/planazo.db` must delete it before running this branch. Tests use tmpdir-scoped DBs and are unaffected. A migration framework is filed as a follow-up.
- **`MAX_STEPS` bumps `4 → 8` in Stage 2.** Multi-event carousels need 1 fetch + up to ~6 `save_event` calls + 1 optional terminal report = 8 headroom. Applies to single-event runs too; the model has no incentive to make extra calls when the brief says "one per distinct event".
- **`save_event`'s LLM-visible schema grows by one property.** `event_index_in_post` is optional (default 0). The tool docstring gains one sentence explaining the multi-event usage; the JSON Schema `schema_for(save_event)` derives the new property automatically.

### Follow-ups

- **Storage schema migration framework.** `PRAGMA user_version` tracking + `src/planazo/storage/migrations/` layout. Filed as a follow-up chore ticket at merge time.
- **Multi-image `_multimodal_hook` widening** (send N carousel slides to the LLM per fetch). Filed as #65 — carousel visual context beyond the first image.
- **Reel / video-frame extraction.** Filed as #66.
- **Scheduler CLI wiring (`planazo-scheduler --tick`).** Filed as #67.
