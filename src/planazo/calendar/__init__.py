"""The calendar bounded context — reference implementation for the approval-gate flow.

Owns the two boundary models validated by the calendar reference tools
(`EventCandidateInput`, `CalendarConfirmationInput`). The tool implementations
themselves still live at `src/tools/tools.py` until they get migrated in a
future v0.2 ticket that lands real Google Calendar wiring (ADR 0002). Locating
the models here anticipates that move without doing it now — the calendar
feature itself is deferred (see ADR 0009 §Follow-ups).
"""

from planazo.calendar.models import (
    CalendarConfirmationInput,
    EventCandidateInput,
    EventSource,
    InvitePolicy,
)

__all__ = [
    "CalendarConfirmationInput",
    "EventCandidateInput",
    "EventSource",
    "InvitePolicy",
]
