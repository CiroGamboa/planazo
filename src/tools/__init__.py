"""Planazo's event-discovery agent tools (observe -> reason -> act -> verify).

Two self-designed tools plug into the generic loop in `planazo.agents.loop`:
`save_event_candidate` (reversible, persists a normalized event to the
local store) and `confirm_and_create_calendar_event` (irreversible, gated —
puts an event on the user's calendar and optionally emails invitees).
"""
