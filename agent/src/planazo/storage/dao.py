"""The domain store's narrow DAO surface — no ORM, two tiers, plain SQL.

**Connection-parameterized primitives** take an explicit `sqlite3.Connection`
as their first argument and never open one themselves, so a caller composing
several of them (push-context assembly, a test against one `":memory:"`
database) does it against a single connection. They let
`sqlite3.IntegrityError` propagate: no LLM tool reaches them, only our own
composition code and tests do, so a `user_id` with no `users` row is a caller
bug and a loud failure is the correct branch for a bug.

**Self-contained wrappers** — `save_event` and `search_events` — take flat
scalar arguments only, open and close their own connection, and return a
JSON-serializable dict that is either a success or a typed error state, exactly
like `tools.tools`'s two tools. They are LLM-reachable, so every failure is a
named branch (AGENTS.md rule 4) rather than an exception: a raw
`sqlite3.Connection` cannot be a tool argument, which is why this tier exists
at all.

Both tiers, the two-tier split, and the `duplicate_event`-instead-of-upsert
decision are recorded in `docs/adr/0003-sqlite-domain-store.md`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from planazo.schemas.domain import (
    Event,
    ExtractionRunIndexEntry,
    PreferenceRecord,
    UserRecord,
)
from planazo.storage import db

# --------------------------------------------------------------------------
# Row <-> model translation.
# --------------------------------------------------------------------------


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    """The row id of the INSERT `cursor` just executed.

    `sqlite3.Cursor.lastrowid` is `int | None` because it is `None` when the
    cursor last ran something other than an INSERT; reaching that here would
    mean the statement above was not the INSERT it looks like.
    """
    row_id = cursor.lastrowid
    if row_id is None:
        raise sqlite3.DatabaseError("INSERT produced no row id")
    return row_id


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        source=row["source"],
        source_url=row["source_url"],
        title=row["title"],
        start_utc=datetime.fromisoformat(row["start_utc"]),
        end_utc=datetime.fromisoformat(row["end_utc"]),
        category=row["category"],
        city=row["city"],
        price_cents=row["price_cents"],
        geo_lat=row["geo_lat"],
        geo_lng=row["geo_lng"],
        confidence=row["confidence"],
        extra=json.loads(row["extra"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
    )


def _user_from_row(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        display_name=row["display_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


# --------------------------------------------------------------------------
# Primitives: explicit connection, IntegrityError propagates.
# --------------------------------------------------------------------------


def insert_event(conn: sqlite3.Connection, event: Event) -> int:
    """Insert `event` and return its new row id.

    `events.source_url` is UNIQUE, so a second insert of the same URL raises
    `sqlite3.IntegrityError`. `save_event` is the tier that turns that into a
    `duplicate_event` branch.
    """
    ingested_at = event.ingested_at or datetime.now(UTC)
    cursor = conn.execute(
        "INSERT INTO events (source, source_url, title, start_utc, end_utc, category, city,"
        " price_cents, geo_lat, geo_lng, confidence, extra, ingested_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.source,
            event.source_url,
            event.title,
            event.start_utc.isoformat(),
            event.end_utc.isoformat(),
            event.category,
            event.city,
            event.price_cents,
            event.geo_lat,
            event.geo_lng,
            event.confidence,
            json.dumps(event.extra),
            ingested_at.isoformat(),
        ),
    )
    conn.commit()
    return _last_row_id(cursor)


def query_events(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    city: str | None = None,
    start_after: datetime | None = None,
    max_results: int = 20,
) -> list[Event]:
    """Return events matching every supplied filter, earliest start first.

    A `None` filter is simply not applied. Timestamps are stored as ISO-8601
    text, which orders and compares chronologically for a single offset.
    """
    clauses: list[str] = []
    params: list[object] = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if city is not None:
        clauses.append("city = ?")
        params.append(city)
    if start_after is not None:
        clauses.append("start_utc >= ?")
        params.append(start_after.isoformat())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max_results)

    rows = conn.execute(
        f"SELECT * FROM events{where} ORDER BY start_utc LIMIT ?", params
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def get_or_create_user(
    conn: sqlite3.Connection, telegram_user_id: str, display_name: str
) -> UserRecord:
    """Return the `users` row for `telegram_user_id`, creating it if absent.

    Idempotent by `telegram_user_id`: a second call with the same id returns
    the existing row (and its id), never a duplicate.
    """
    existing = conn.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if existing is not None:
        return _user_from_row(existing)

    created_at = datetime.now(UTC)
    record = UserRecord(
        telegram_user_id=telegram_user_id, display_name=display_name, created_at=created_at
    )
    cursor = conn.execute(
        "INSERT INTO users (telegram_user_id, display_name, created_at) VALUES (?, ?, ?)",
        (record.telegram_user_id, record.display_name, created_at.isoformat()),
    )
    conn.commit()
    return record.model_copy(update={"id": _last_row_id(cursor)})


def get_preferences(conn: sqlite3.Connection, user_id: int) -> list[PreferenceRecord]:
    """Return every preference row for `user_id`, by key.

    An unknown `user_id` yields `[]` — this is a read, not a constraint
    violation.
    """
    rows = conn.execute(
        "SELECT * FROM preferences WHERE user_id = ? ORDER BY key", (user_id,)
    ).fetchall()
    return [
        PreferenceRecord(
            user_id=row["user_id"],
            key=row["key"],
            value=row["value"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        for row in rows
    ]


def set_preference(
    conn: sqlite3.Connection, user_id: int, key: str, value: str
) -> PreferenceRecord:
    """Upsert one preference for `user_id` and return the stored row.

    `preferences` is keyed on `(user_id, key)`, so a plain second INSERT would
    raise; `ON CONFLICT ... DO UPDATE` replaces the value instead. A `user_id`
    with no `users` row raises `sqlite3.IntegrityError` — see this module's
    docstring on why that stays loud.
    """
    updated_at = datetime.now(UTC)
    record = PreferenceRecord(user_id=user_id, key=key, value=value, updated_at=updated_at)
    conn.execute(
        "INSERT INTO preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (record.user_id, record.key, record.value, updated_at.isoformat()),
    )
    conn.commit()
    return record


def record_extraction_run(conn: sqlite3.Connection, entry: ExtractionRunIndexEntry) -> int:
    """Append one pointer into the Extractor's run log; return its row id."""
    started_at = entry.started_at or datetime.now(UTC)
    cursor = conn.execute(
        "INSERT INTO extraction_runs_index (run_id, user_id, url, started_at) VALUES (?, ?, ?, ?)",
        (entry.run_id, entry.user_id, entry.url, started_at.isoformat()),
    )
    conn.commit()
    return _last_row_id(cursor)


def list_extraction_runs(conn: sqlite3.Connection, user_id: int) -> list[ExtractionRunIndexEntry]:
    """Return every indexed extraction run for `user_id`, earliest first."""
    rows = conn.execute(
        "SELECT * FROM extraction_runs_index WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [
        ExtractionRunIndexEntry(
            id=row["id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            url=row["url"],
            started_at=datetime.fromisoformat(row["started_at"]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Tool-ready wrappers: flat scalars, own connection, typed error states.
# --------------------------------------------------------------------------


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
