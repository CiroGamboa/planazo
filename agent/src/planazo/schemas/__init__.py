"""Pydantic v2 boundary models for Planazo's data contracts.

`events.py` holds two neighbouring tool-boundary surfaces (AGENTS.md rule 1):
`EventCandidateInput`/`CalendarConfirmationInput` validate the payloads the
calendar reference tools receive from the LLM, and `SearchIntent` is the
structured output the query interpreter emits from a free-text `/find`
query. `domain.py` holds the SQLite domain store's remaining row models —
`Event` and `ExtractionRunIndexEntry`.
`memory.py` holds the JSON docstore's own row shapes (`Fact`, `Note`) plus
the `MemoryScopeRequest` that validates the identity selecting their
directory — the memory store's rows, not entries in that table. `ApprovalDecision`
lives beside its repository under `planazo/approval/` per the bounded-context
layout ([ADR 0008](../../../../docs/adr/0008-domain-driven-module-layout.md)).
Later tickets add the remaining entities from that table (UserPreferences,
RawEventCandidate, ExtractionError, RankedEventList, CalendarDraft) as they
land.
"""
