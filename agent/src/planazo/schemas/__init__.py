"""Pydantic v2 boundary models for Planazo's data contracts.

`events.py` holds the models validated at the tool boundary (AGENTS.md rule
1) for the calendar reference tools. `domain.py` holds the SQLite domain
store's row models — `Event` and `ApprovalDecision` from AGENTS.md's
data-contract table, plus `UserRecord`, `PreferenceRecord`, and
`ExtractionRunIndexEntry`. Later tickets add the remaining entities from that
table (UserRequest, UserPreferences, RawEventCandidate, ExtractionError,
RankedEventList, CalendarDraft) as they land.
"""
