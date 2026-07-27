"""Catalog LLM tool adapters — `save_event` and `search_events`.

The tool-ready tier of the two-tier pattern (ADR 0003): flat scalar
arguments only, own connection open/close, typed error branches instead of
propagating exceptions. The names + signatures + error branch shape are
pinned by ADR 0003 as a cross-ticket contract (M3's Extraction Agent
imports `save_event` by name).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.catalog.repository import _event_from_row, insert_event, query_events
from planazo.storage import db


def save_event(
    title: str,
    category: str,
    source: str,
    source_url: str,
    start_utc: str,
    end_utc: str,
    city: str,
    confidence: float,
    price_cents: int = 0,
    geo_lat: float = 0.0,
    geo_lng: float = 0.0,
) -> dict[str, object]:
    """Persist one normalized event to the shared event store.

    Call this AFTER an extraction or source step has produced an event with a
    resolved title, category, city, and ISO-8601 `start_utc`/`end_utc`, so it
    becomes searchable through `search_events`. `source_url` is the event's
    natural key: it must be the real page the event came from, and each URL can
    be saved once — a second save of the same URL comes back as
    `duplicate_event` with the id of the row that already exists, so read that
    branch instead of retrying. Do NOT call this with raw, unnormalized scraped
    text as `title` or `city`, and do NOT call it to look up events (it has no
    read-back behaviour — use `search_events`).
    """
    try:
        parsed_start = datetime.fromisoformat(start_utc)
        parsed_end = datetime.fromisoformat(end_utc)
    except ValueError as exc:
        return {"error_type": "invalid_event_data", "message": f"invalid timestamp: {exc}"}

    try:
        event = Event(
            source=source,
            source_url=source_url,
            title=title,
            start_utc=parsed_start,
            end_utc=parsed_end,
            category=category,
            city=city,
            price_cents=price_cents,
            geo_lat=geo_lat,
            geo_lng=geo_lng,
            confidence=confidence,
        )
    except ValidationError as exc:
        return {"error_type": "invalid_event_data", "message": str(exc)}

    conn = db.connect()
    try:
        try:
            event_db_id = insert_event(conn, event)
        except sqlite3.IntegrityError as exc:
            duplicate = conn.execute(
                "SELECT id FROM events WHERE source_url = ?", (event.source_url,)
            ).fetchone()
            if duplicate is None:
                return {"error_type": "invalid_event_data", "message": str(exc)}
            return {
                "error_type": "duplicate_event",
                "message": f"source_url {event.source_url!r} already has a row",
                "event_db_id": int(duplicate["id"]),
            }
        # VERIFY: read the row back rather than trust the write just made.
        persisted = _event_from_row(
            conn.execute("SELECT * FROM events WHERE id = ?", (event_db_id,)).fetchone()
        )
    finally:
        conn.close()

    return {"saved": persisted.model_dump(mode="json"), "event_db_id": event_db_id}


def search_events(
    category: str = "", city: str = "", start_after: str = "", max_results: int = 20
) -> dict[str, object]:
    """Search the shared event store for events matching the given filters.

    Call this to find events that are already stored, for example to answer
    "what tech events are on in Barcelona this week": pass `category` and/or
    `city` to narrow by those fields and an ISO-8601 `start_after` to exclude
    anything starting earlier. An empty string means "no filter on that
    field", so calling this with no arguments returns the earliest
    `max_results` events. An empty `events` list means nothing stored matches,
    not that the search failed. Do NOT call this to save an event (it has no
    write behaviour).
    """
    parsed_start_after: datetime | None = None
    if start_after:
        try:
            parsed_start_after = datetime.fromisoformat(start_after)
        except ValueError as exc:
            return {
                "error_type": "invalid_search_filter",
                "message": f"invalid start_after: {exc}",
            }

    # SQLite reads a negative LIMIT as "no limit", so a non-positive cap here
    # would hand back the whole table instead of the bounded page the argument
    # asks for.
    if max_results < 1:
        return {
            "error_type": "invalid_search_filter",
            "message": f"max_results must be at least 1, got {max_results}",
        }

    conn = db.connect()
    try:
        found = query_events(
            conn,
            category=category or None,
            city=city or None,
            start_after=parsed_start_after,
            max_results=max_results,
        )
    finally:
        conn.close()

    return {"events": [event.model_dump(mode="json") for event in found], "total": len(found)}
