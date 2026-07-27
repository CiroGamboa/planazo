"""Two real, self-designed tools for Planazo's event-discovery agent.

Both are the actual persistence layer described in
`docs/PLANAZO-PROJECT-CONTEXT.md` ("the event database can be stored
locally in JSON or SQLite... the Google Calendar tool can create a
draft/staged event"), not a class-exercise stand-in next to some other
"real" system:

- `save_event_candidate` touches a local JSON file and persists state. It
  is reversible (re-saving a corrected candidate is just another write);
  it does not require approval before dispatch.
- `confirm_and_create_calendar_event` also persists state, but is treated
  as irreversible: it is the action that puts an event on the user's
  calendar and, when requested, emails other people about it — an action
  visible to a third party (AGENTS.md rule 3). An agent doing that on its
  own judgment is exactly what that rule exists to prevent, so callers gate
  this tool by including its name in an `ApprovalGate` before dispatch (see
  `planazo.agents.loop.ApprovalGate`).

Both tools validate their input through the Pydantic v2 boundary models in
`planazo.schemas.events` (AGENTS.md rule 1) and return a typed error state
— an `error_type` key in the result — rather than raising or silently
persisting a partial/unreliable record (AGENTS.md rule 4). A tool that
raises anyway (a disk error, a bug) is still caught, but one layer up, by
the agent loop itself (`planazo.agents.loop.run_loop`'s generic dispatch
try/except): that generic catch is what turns an actual tool failure into
its own branch instead of letting it look like valid data. The typed
`error_type` results here are this module's own, narrower layer, for input
problems specifically.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import ValidationError

from planazo.schemas.events import CalendarConfirmationInput, EventCandidateInput
from tools.schema import schema_for

# --------------------------------------------------------------------------
# Tool 1: save_event_candidate - reversible, local file, persists state.
# --------------------------------------------------------------------------

CANDIDATES_PATH = Path("var/event_candidates.json")

# Below this, the project context calls the extraction "not reliable enough
# to show to the user" — so it is not reliable enough to persist either.
LOW_CONFIDENCE_THRESHOLD = 0.3


class EventCandidateEntry(TypedDict):
    id: int
    event_id: str
    title: str
    category: str
    source: str
    start_time: str
    location: str
    confidence: float


def _load_candidates() -> list[EventCandidateEntry]:
    if not CANDIDATES_PATH.exists():
        return []
    entries: list[EventCandidateEntry] = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    return entries


def _save_candidates(entries: list[EventCandidateEntry]) -> None:
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def save_event_candidate(
    event_id: str,
    title: str,
    category: Literal["tech", "cultural", "music", "networking", "sports", "other"],
    source: Literal["eventbrite", "meetup", "instagram", "manual"],
    start_time: str,
    location: str,
    confidence: float,
) -> dict[str, object]:
    """Persist one normalized event candidate to the local event store.

    Call this AFTER a source tool or extraction step has produced a
    candidate with a resolved title, category, ISO-8601 `start_time`, and
    location, so it is saved for ranking and later selection by
    `confirm_and_create_calendar_event`. Do NOT call this with raw,
    unnormalized scraped text as `title` or `location` — normalize it
    first — and do NOT call it to look up previously saved candidates (it
    has no read-back behaviour).
    """
    try:
        parsed_start_time = datetime.fromisoformat(start_time)
    except ValueError as exc:
        return {"error_type": "invalid_event_data", "message": f"invalid start_time: {exc}"}

    try:
        candidate = EventCandidateInput(
            event_id=event_id,
            title=title,
            category=category,
            source=source,
            start_time=parsed_start_time,
            location=location,
            confidence=confidence,
        )
    except ValidationError as exc:
        return {"error_type": "invalid_event_data", "message": str(exc)}

    if candidate.confidence < LOW_CONFIDENCE_THRESHOLD:
        return {
            "error_type": "low_confidence_extraction",
            "message": (
                f"confidence {candidate.confidence} is below "
                f"{LOW_CONFIDENCE_THRESHOLD}; not reliable enough to save"
            ),
        }

    entries = _load_candidates()
    next_id = max((entry["id"] for entry in entries), default=0) + 1
    entry: EventCandidateEntry = {
        "id": next_id,
        "event_id": candidate.event_id,
        "title": candidate.title,
        "category": candidate.category,
        "source": candidate.source,
        "start_time": candidate.start_time.isoformat(),
        "location": candidate.location,
        "confidence": candidate.confidence,
    }
    entries.append(entry)
    _save_candidates(entries)

    # VERIFY: re-read the file rather than trust the write just made.
    persisted = _load_candidates()
    return {"saved": entry, "total_candidates": len(persisted)}


# --------------------------------------------------------------------------
# Tool 2: confirm_and_create_calendar_event - irreversible, gated.
# --------------------------------------------------------------------------

CALENDAR_EVENTS_PATH = Path("var/calendar_events.json")


class CalendarEventEntry(TypedDict):
    id: int
    event_id: str
    title: str
    start_time: str
    location: str
    notify_invitees: str
    invitee_emails: list[str]


def _load_calendar_events() -> list[CalendarEventEntry]:
    if not CALENDAR_EVENTS_PATH.exists():
        return []
    entries: list[CalendarEventEntry] = json.loads(CALENDAR_EVENTS_PATH.read_text(encoding="utf-8"))
    return entries


def _save_calendar_events(entries: list[CalendarEventEntry]) -> None:
    CALENDAR_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_EVENTS_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def confirm_and_create_calendar_event(
    event_id: str,
    notify_invitees: Literal["none", "email_invite"],
    invitee_emails: str = "",
) -> dict[str, object]:
    """Create the user's calendar entry for a previously saved event candidate.

    Call this ONLY after the user has explicitly confirmed, in chat, that
    THIS SPECIFIC event (identified by `event_id`, as returned by
    `save_event_candidate`) should go on their calendar — this is the
    action that actually creates the calendar entry and, when
    `notify_invitees="email_invite"`, emails the people listed in the
    comma-separated `invitee_emails`. It is gated behind human approval
    before dispatch (see `planazo.agents.loop.ApprovalGate`). Do NOT call
    this while only browsing or ranking candidates, and do NOT call it to
    check whether an event was already confirmed (it has no read-back
    behaviour).
    """
    emails = tuple(email.strip() for email in invitee_emails.split(",") if email.strip())

    try:
        confirmation = CalendarConfirmationInput(
            event_id=event_id, notify_invitees=notify_invitees, invitee_emails=emails
        )
    except ValidationError as exc:
        return {"error_type": "invalid_confirmation_data", "message": str(exc)}

    if confirmation.notify_invitees == "email_invite" and not confirmation.invitee_emails:
        return {
            "error_type": "missing_invitees",
            "message": "notify_invitees is 'email_invite' but invitee_emails is empty",
        }

    candidate = next(
        (c for c in _load_candidates() if c["event_id"] == confirmation.event_id), None
    )
    if candidate is None:
        return {
            "error_type": "event_not_found",
            "message": f"no saved candidate with event_id {confirmation.event_id!r}",
        }

    entries = _load_calendar_events()
    next_id = max((entry["id"] for entry in entries), default=0) + 1
    entry: CalendarEventEntry = {
        "id": next_id,
        "event_id": candidate["event_id"],
        "title": candidate["title"],
        "start_time": candidate["start_time"],
        "location": candidate["location"],
        "notify_invitees": confirmation.notify_invitees,
        "invitee_emails": list(confirmation.invitee_emails),
    }
    entries.append(entry)
    _save_calendar_events(entries)

    # VERIFY: re-read the file rather than trust the write just made.
    persisted = _load_calendar_events()
    return {
        "created": entry,
        "total_confirmed": len(persisted),
        "invitees_notified": confirmation.notify_invitees == "email_invite",
    }


# --------------------------------------------------------------------------
# Registry wiring.
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [schema_for(save_event_candidate), schema_for(confirm_and_create_calendar_event)]

TOOL_REGISTRY: dict[str, Callable[..., dict[str, object]]] = {
    "save_event_candidate": save_event_candidate,
    "confirm_and_create_calendar_event": confirm_and_create_calendar_event,
}

# Irreversibility decides where a gate goes (AGENTS.md rule 3): saving a
# candidate is reversible (re-saving a correction is just another write); a
# confirmed calendar event is visible to a third party and, when invitees
# are notified, emails other people — irreversible in effect even though
# the JSON row itself could technically be deleted.
IRREVERSIBLE_TOOLS = {"confirm_and_create_calendar_event"}
