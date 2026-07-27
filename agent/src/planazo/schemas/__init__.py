"""Pydantic v2 boundary models still colocated under `schemas/`.

Only the tool-boundary surfaces without an owning bounded context yet live
here (AGENTS.md rule 1):

- `events.py` — `EventCandidateInput`/`CalendarConfirmationInput` for the
  calendar reference tools, plus `SearchIntent` (the query interpreter's
  parsed `/find` output).
- `memory.py` — `Fact`, `Note`, and `MemoryScopeRequest` for the JSON
  docstore, colocated with `planazo.memory`'s own module.

Aggregate models that already have a bounded-context home live there instead
(see [ADR 0008](../../../../docs/adr/0008-domain-driven-module-layout.md)):
`Event` + `ExtractionRunIndexEntry` in `planazo.catalog.models`;
`UserRecord` + `PreferenceRecord` in `planazo.identity.models`;
`ApprovalDecision` in `planazo.approval.models`.

Later tickets add the remaining entities from AGENTS.md's Data Contracts
table (UserPreferences, RawEventCandidate, ExtractionError, RankedEventList,
CalendarDraft) in the bounded context that owns each.
"""
