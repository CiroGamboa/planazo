"""Seed the domain store with 25 realistic Barcelona events for E2E demos.

Idempotent: a second run against the same DB inserts nothing new — the
composite `UNIQUE(source_url, event_index_in_post)` on `events` turns
every second-attempt row into an `sqlite3.IntegrityError` that this
script catches and counts as "skipped". A fresh DB inserts 25 rows and
prints `Inserted 25 events, skipped 0 (already present).`; a re-run
against the same DB prints `Inserted 0 events, skipped 25 (already
present).`.

Every row carries `source = "seed"` so operators can distinguish these
from real `source = "instagram"` rows produced by the scheduled
Extractor. The `source_url` is a synthetic `seed://event/<slug>` scheme
— guaranteed unique per event and never collides with a real
Instagram permalink.

Category mix (matches `query.models.EventCategory`):
- tech          — 5 rows
- cultural      — 5 rows
- music         — 5 rows
- networking    — 3 rows
- sports        — 4 rows
- other         — 3 rows

Start times span the next 30 days (relative to today) so a query for
"this weekend" or "next week" always finds candidates. Venue names,
coordinates, and price cents are varied so the ranker's proximity /
freshness / preference axes all have something to differentiate on
when it lands in a follow-up ticket.

Run: `uv run python scripts/seed_events.py`.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta

from planazo.catalog.models import Event
from planazo.catalog.repository import insert_event
from planazo.storage import db

# Barcelona venue coordinates — a handful of well-known spots so the
# ranker's proximity axis has diversity to differentiate on. Each tuple
# is (venue_name, lat, lng).
_BARCELONA_VENUES: dict[str, tuple[float, float]] = {
    "Sala Apolo": (41.3757, 2.1652),
    "Razzmatazz": (41.3982, 2.1902),
    "MACBA": (41.3830, 2.1670),
    "Poble Espanyol": (41.3689, 2.1489),
    "Parc de la Ciutadella": (41.3877, 2.1866),
    "Sant Antoni Market": (41.3789, 2.1571),
    "CCCB": (41.3823, 2.1673),
    "Palau de la Musica": (41.3874, 2.1750),
    "Camp Nou": (41.3809, 2.1228),
    "Pier 01 Barcelona Tech City": (41.3781, 2.1897),
}


def _event(
    *,
    slug: str,
    title: str,
    category: str,
    days_from_now: int,
    duration_hours: int,
    price_cents: int,
    venue: str | None,
    now: datetime,
    tags: list[str] | None = None,
    description: str | None = None,
) -> Event:
    """Build one seeded `Event` deterministically from its slug + offset."""
    start = now + timedelta(days=days_from_now, hours=19)  # 19:00 UTC ≈ 21:00 CEST
    end = start + timedelta(hours=duration_hours)
    coords = _BARCELONA_VENUES.get(venue) if venue else None
    return Event(
        source="seed",
        source_url=f"seed://event/{slug}",
        title=title,
        start_utc=start,
        end_utc=end,
        category=category,  # type: ignore[arg-type]  # EventCategory Literal is enforced at construction.
        city="Barcelona",
        price_cents=price_cents,
        geo_lat=coords[0] if coords else None,
        geo_lng=coords[1] if coords else None,
        confidence=0.9,
        venue_name=venue,
        tags=tags or [],
        description=description,
        language="es",
    )


def _catalog(now: datetime) -> list[Event]:
    """The 25-event catalog — one deterministic list, all fields spelled out."""
    return [
        # ---- tech (5) ----
        _event(
            slug="ai-meetup-2026",
            title="Barcelona AI Builders Meetup",
            category="tech",
            days_from_now=2,
            duration_hours=3,
            price_cents=0,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["ai", "meetup"],
            description="Monthly meetup for AI builders — lightning talks and demos.",
        ),
        _event(
            slug="rust-workshop-2026",
            title="Introduction to Rust Systems Programming",
            category="tech",
            days_from_now=5,
            duration_hours=4,
            price_cents=2500,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["rust", "workshop"],
        ),
        _event(
            slug="devops-conf-2026",
            title="DevOps Barcelona Conference",
            category="tech",
            days_from_now=9,
            duration_hours=8,
            price_cents=12000,
            venue="CCCB",
            now=now,
            tags=["devops", "conference"],
        ),
        _event(
            slug="startup-pitch-night",
            title="Startup Pitch Night",
            category="tech",
            days_from_now=14,
            duration_hours=3,
            price_cents=1000,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["startup", "pitch"],
        ),
        _event(
            slug="women-in-tech",
            title="Women in Tech Barcelona",
            category="tech",
            days_from_now=20,
            duration_hours=2,
            price_cents=0,
            venue=None,
            now=now,
            tags=["diversity", "networking"],
        ),
        # ---- cultural (5) ----
        _event(
            slug="macba-exhibition-2026",
            title="Contemporary Art Exhibition at MACBA",
            category="cultural",
            days_from_now=1,
            duration_hours=4,
            price_cents=1200,
            venue="MACBA",
            now=now,
            tags=["exhibition", "art"],
        ),
        _event(
            slug="modernisme-tour",
            title="Guided Walking Tour: Barcelona Modernisme",
            category="cultural",
            days_from_now=4,
            duration_hours=3,
            price_cents=1800,
            venue=None,
            now=now,
            tags=["tour", "architecture"],
        ),
        _event(
            slug="cccb-film-fest",
            title="Independent Film Festival at CCCB",
            category="cultural",
            days_from_now=8,
            duration_hours=5,
            price_cents=800,
            venue="CCCB",
            now=now,
            tags=["film", "festival"],
        ),
        _event(
            slug="poble-espanyol-crafts",
            title="Traditional Catalan Crafts Fair",
            category="cultural",
            days_from_now=12,
            duration_hours=6,
            price_cents=500,
            venue="Poble Espanyol",
            now=now,
            tags=["crafts", "fair"],
        ),
        _event(
            slug="theatre-night-2026",
            title="Contemporary Theatre Night",
            category="cultural",
            days_from_now=22,
            duration_hours=3,
            price_cents=2400,
            venue="Palau de la Musica",
            now=now,
            tags=["theatre"],
        ),
        # ---- music (5) ----
        _event(
            slug="apolo-techno-friday",
            title="Techno Friday at Sala Apolo",
            category="music",
            days_from_now=3,
            duration_hours=6,
            price_cents=1800,
            venue="Sala Apolo",
            now=now,
            tags=["techno", "club"],
        ),
        _event(
            slug="razzmatazz-live",
            title="Indie Live Session at Razzmatazz",
            category="music",
            days_from_now=6,
            duration_hours=4,
            price_cents=2200,
            venue="Razzmatazz",
            now=now,
            tags=["indie", "live"],
        ),
        _event(
            slug="palau-classical",
            title="Classical Chamber Music at Palau de la Musica",
            category="music",
            days_from_now=10,
            duration_hours=2,
            price_cents=3500,
            venue="Palau de la Musica",
            now=now,
            tags=["classical", "chamber"],
        ),
        _event(
            slug="apolo-house-saturday",
            title="House Saturday at Sala Apolo",
            category="music",
            days_from_now=17,
            duration_hours=7,
            price_cents=2000,
            venue="Sala Apolo",
            now=now,
            tags=["house", "club"],
        ),
        _event(
            slug="parc-free-concert",
            title="Free Outdoor Concert Series",
            category="music",
            days_from_now=25,
            duration_hours=3,
            price_cents=0,
            venue="Parc de la Ciutadella",
            now=now,
            tags=["outdoor", "free"],
        ),
        # ---- networking (3) ----
        _event(
            slug="expat-mixer",
            title="Barcelona Expat Networking Mixer",
            category="networking",
            days_from_now=7,
            duration_hours=3,
            price_cents=1000,
            venue=None,
            now=now,
            tags=["expat", "mixer"],
        ),
        _event(
            slug="founders-brunch",
            title="Founders Brunch — Angel Investors Circle",
            category="networking",
            days_from_now=15,
            duration_hours=2,
            price_cents=3000,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["founders", "brunch"],
        ),
        _event(
            slug="freelancers-meetup",
            title="Freelancers Meetup Barcelona",
            category="networking",
            days_from_now=27,
            duration_hours=2,
            price_cents=0,
            venue=None,
            now=now,
            tags=["freelance", "meetup"],
        ),
        # ---- sports (4) ----
        _event(
            slug="fc-barcelona-match",
            title="FC Barcelona Home Match",
            category="sports",
            days_from_now=11,
            duration_hours=2,
            price_cents=5500,
            venue="Camp Nou",
            now=now,
            tags=["football", "match"],
        ),
        _event(
            slug="parc-morning-run",
            title="Sunday Morning Run in Parc de la Ciutadella",
            category="sports",
            days_from_now=13,
            duration_hours=1,
            price_cents=0,
            venue="Parc de la Ciutadella",
            now=now,
            tags=["running", "outdoor"],
        ),
        _event(
            slug="yoga-in-the-park",
            title="Outdoor Yoga in the Park",
            category="sports",
            days_from_now=18,
            duration_hours=1,
            price_cents=500,
            venue="Parc de la Ciutadella",
            now=now,
            tags=["yoga", "wellness"],
        ),
        _event(
            slug="urban-cycling-tour",
            title="Urban Cycling Discovery Tour",
            category="sports",
            days_from_now=24,
            duration_hours=3,
            price_cents=2500,
            venue=None,
            now=now,
            tags=["cycling", "tour"],
        ),
        # ---- other (3) ----
        _event(
            slug="sant-antoni-farmers",
            title="Sant Antoni Farmers Market Sunday",
            category="other",
            days_from_now=16,
            duration_hours=5,
            price_cents=0,
            venue="Sant Antoni Market",
            now=now,
            tags=["market", "food"],
        ),
        _event(
            slug="street-food-fest",
            title="International Street Food Festival",
            category="other",
            days_from_now=19,
            duration_hours=8,
            price_cents=800,
            venue="Poble Espanyol",
            now=now,
            tags=["food", "festival"],
        ),
        _event(
            slug="wine-tasting",
            title="Catalan Wine Tasting Evening",
            category="other",
            days_from_now=28,
            duration_hours=2,
            price_cents=3200,
            venue=None,
            now=now,
            tags=["wine", "tasting"],
        ),
    ]


def _insert_or_skip(conn: sqlite3.Connection, event: Event) -> bool:
    """Attempt to insert `event`; return True if inserted, False if duplicate.

    `insert_event` propagates `sqlite3.IntegrityError` on a
    `UNIQUE(source_url, event_index_in_post)` collision — that is the
    idempotency signal this script relies on. Any other
    `IntegrityError` (an FK violation, a bad CHECK) is re-raised as an
    operator bug because it would signal a corrupt seed catalog rather
    than a benign re-run.
    """
    try:
        insert_event(conn, event)
        return True
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "UNIQUE" not in message:
            raise
        return False


def main() -> int:
    """Seed the catalog and print a one-line report; return process exit code."""
    now = datetime.now(UTC)
    catalog = _catalog(now)
    conn = db.connect()
    try:
        inserted = 0
        skipped = 0
        for event in catalog:
            if _insert_or_skip(conn, event):
                inserted += 1
            else:
                skipped += 1
    finally:
        conn.close()
    print(f"Inserted {inserted} events, skipped {skipped} (already present).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
