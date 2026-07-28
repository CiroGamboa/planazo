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
from planazo.query.models import EventCategory
from planazo.storage import db


def _text_or_none(value: str) -> str | None:
    """Empty-string sentinel ↔ `None`. Whitespace-only strings collapse too."""
    stripped = value.strip()
    return stripped or None


def save_event(
    title: str,
    category: EventCategory,
    source: str,
    source_url: str,
    start_utc: str,
    end_utc: str,
    city: str,
    confidence: float,
    price_cents: int = 0,
    geo_lat: float = 0.0,
    geo_lng: float = 0.0,
    event_index_in_post: int = 0,
    source_account: str = "",
    venue_name: str = "",
    venue_address: str = "",
    organizer: str = "",
    tags: list[str] | None = None,
    description: str = "",
    ticket_url: str = "",
    image_url: str = "",
    language: str = "",
    recurring: bool = False,
) -> dict[str, object]:
    """Persist one normalized event to the shared event store.

    Call this AFTER an extraction or source step has produced an event with a
    resolved title, category, city, and ISO-8601 `start_utc`/`end_utc`, so it
    becomes searchable through `search_events`. The natural key is the
    composite `(source_url, event_index_in_post)`: `source_url` must be the
    real page the event came from, and each `(source_url, event_index_in_post)`
    pair can be saved once — a second save with the same pair comes back as
    `duplicate_event` with the id of the row that already exists, so read that
    branch instead of retrying. For a single post that announces multiple
    distinct events, call this once per event with `event_index_in_post` =
    `0`, `1`, `2`, ...; the composite `(source_url, event_index_in_post)` is
    the natural key. Do NOT call this with raw, unnormalized scraped text as
    `title` or `city`, and do NOT call it to look up events (it has no
    read-back behaviour).

    `category` is constrained to the shared `EventCategory` literal set;
    values outside `{"tech", "cultural", "music", "networking", "sports",
    "other"}` come back as `invalid_event_data`.

    Rich-domain fields (all optional, default empty):
    - `source_account` — the source-account handle that posted the event
      (Instagram, for example, exposes it as `author_handle` on
      `fetch_instagram_post`'s return; copy that value into this field).
    - `venue_name` — the named venue (e.g. `"Sala Apolo"`).
    - `venue_address` — the venue's street/address.
    - `organizer` — the promoter or event-executor identity, distinct from
      the venue.
    - `tags` — free-form tags/genres as a list of strings (empty means no
      tags known); persisted as a JSON array under the same discipline as
      `extra`.
    - `description` — the LLM's paraphrase of the caption + flyer content;
      never a byte-for-byte copy of a scraped caption (AGENTS.md rule 2).
    - `ticket_url` — the canonical ticket-purchase URL when the post lists
      one.
    - `image_url` — the cover/flyer image URL for rendering downstream.
    - `language` — ISO-639 code where determinable (`"es"`, `"ca"`, `"en"`).
    - `recurring` — `True` marks the row as a recurring/series stub for
      future use; leave `False` for single-instance events.
    """
    if event_index_in_post < 0:
        return {
            "error_type": "invalid_event_data",
            "message": (f"event_index_in_post must be non-negative, got {event_index_in_post}"),
        }

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
            event_index_in_post=event_index_in_post,
            source_account=_text_or_none(source_account),
            venue_name=_text_or_none(venue_name),
            venue_address=_text_or_none(venue_address),
            organizer=_text_or_none(organizer),
            tags=list(tags) if tags else [],
            description=_text_or_none(description),
            ticket_url=_text_or_none(ticket_url),
            image_url=_text_or_none(image_url),
            language=_text_or_none(language),
            recurring=recurring,
        )
    except ValidationError as exc:
        return {"error_type": "invalid_event_data", "message": str(exc)}

    conn = db.connect()
    try:
        try:
            event_db_id = insert_event(conn, event)
        except sqlite3.IntegrityError as exc:
            duplicate = conn.execute(
                "SELECT id FROM events WHERE source_url = ? AND event_index_in_post = ?",
                (event.source_url, event.event_index_in_post),
            ).fetchone()
            if duplicate is None:
                return {"error_type": "invalid_event_data", "message": str(exc)}
            return {
                "error_type": "duplicate_event",
                "message": (
                    f"(source_url={event.source_url!r},"
                    f" event_index_in_post={event.event_index_in_post}) already has a row"
                ),
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
    category: str = "",
    city: str = "",
    start_after: str = "",
    venue_name: str = "",
    tag: str = "",
    title_contains: str = "",
    budget_cents_max: int = -1,
    max_results: int = 20,
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

    Extra filters (all optional; sentinel = no filter):
    - `venue_name` — exact match on the venue name (empty = no filter).
    - `tag` — a single tag; a row matches when its JSON `tags` array
      contains this exact value (empty = no filter).
    - `title_contains` — case-sensitive SQL `LIKE '%X%'` substring match on
      `title` (empty = no filter).
    - `budget_cents_max` — upper bound on `price_cents` (inclusive); pass
      `-1` for no filter, `0` to search free events only.
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

    if budget_cents_max < -1:
        return {
            "error_type": "invalid_search_filter",
            "message": (
                f"budget_cents_max must be -1 (no filter) or non-negative, got {budget_cents_max}"
            ),
        }

    conn = db.connect()
    try:
        found = query_events(
            conn,
            category=category or None,
            city=city or None,
            start_after=parsed_start_after,
            venue_name=venue_name or None,
            tag=tag or None,
            title_contains=title_contains or None,
            budget_cents_max=budget_cents_max if budget_cents_max >= 0 else None,
            max_results=max_results,
        )
    finally:
        conn.close()

    return {"events": [event.model_dump(mode="json") for event in found], "total": len(found)}
