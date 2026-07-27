"""Catalog repository — narrow, connection-parameterized SQL for `events` + `extraction_runs_index`.

Matches the two-tier pattern documented in ADR 0003: functions here take an
explicit `sqlite3.Connection` and never open one themselves. `save_event` and
`search_events` in the sibling `tools.py` are the LLM-reachable tier that
opens/closes its own connection and returns typed error branches instead of
propagating exceptions.

`sqlite3.IntegrityError` propagates from the primitives: no LLM tool reaches
them, only our own composition code and `tools.py` do; a duplicate
`source_url` there is a caller bug (or the natural signal `save_event` turns
into a `duplicate_event` branch). See ADR 0003's "loud primitives, typed
wrappers" section.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from planazo.catalog.models import Event, ExtractionRunIndexEntry


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
