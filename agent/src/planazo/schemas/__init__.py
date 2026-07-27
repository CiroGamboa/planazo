"""Pydantic v2 boundary models for Planazo's data contracts.

`events.py` holds the models validated at the tool boundary (AGENTS.md rule
1) for the event-discovery agent's tools. Later tickets add the remaining
entities from AGENTS.md's data-contract table (UserRequest,
UserPreferences, RawEventCandidate, Event, ExtractionError,
RankedEventList, CalendarDraft, ApprovalDecision) as they land.
"""
