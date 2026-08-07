"""Seed the domain store with 60 realistic Barcelona events for E2E demos.

Idempotent: a second run against the same DB inserts nothing new — the
composite `UNIQUE(source_url, event_index_in_post)` on `events` turns
every second-attempt row into an `sqlite3.IntegrityError` that this
script catches and counts as "skipped". A fresh DB inserts 60 rows and
prints `Inserted 60 events, skipped 0 (already present).`; a re-run
against the same DB prints `Inserted 0 events, skipped 60 (already
present).`.

Every row carries `source = "seed"` so operators can distinguish these
from real `source = "instagram"` rows produced by the scheduled
Extractor. The `source_url` is a synthetic `seed://event/<slug>` scheme
— guaranteed unique per event and never collides with a real
Instagram permalink.

Category mix (matches `query.models.EventCategory`) — evenly balanced
so any single-category query has a comparable candidate pool:

- tech          — 10 rows
- cultural      — 10 rows
- music         — 10 rows
- networking    — 10 rows
- sports        — 10 rows
- other         — 10 rows

Start times span the next 30 days (relative to today) so a query for
"this weekend" or "next week" always finds candidates. Venue names,
coordinates, and price cents are varied so the ranker's proximity /
freshness / preference axes all have something to differentiate on.
Rows carry short descriptions so the RAG retriever's embedding + BM25
paths have enough textual signal to rank by meaning as well as by
category.

Same-day (`days_from_now=0`) picks — six of the 60 events are anchored
to *today* 19:00 UTC so a "tonight" or "this evening" query always
has candidates in its window. Chosen for demo coverage across four
categories: `ai-meetup-2026` + `rust-workshop-2026` (tech),
`apolo-techno-friday` + `jazz-apolo` (music),
`macba-exhibition-2026` (cultural), `basketball-pickup` (sports).
Idempotency caveat: the seed inserts by `source_url` uniqueness, so a
re-seed against an existing DB keeps the *original* start_utc of these
rows. To refresh today's date on a returning-user machine, delete
`var/planazo.db` before re-seeding.

Run: `uv run python scripts/seed_events.py`.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, time, timedelta

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
    "Parc Güell": (41.4145, 2.1527),
    "Sagrada Familia": (41.4036, 2.1744),
    "Barceloneta Beach": (41.3762, 2.1899),
    "El Born Cultural Centre": (41.3844, 2.1815),
    "Casa Batlló": (41.3917, 2.1650),
    "Fabra i Coats": (41.4373, 2.1861),
    "Antiga Fàbrica Estrella Damm": (41.4055, 2.1836),
    "MNAC": (41.3689, 2.1536),
    "La Rambla": (41.3818, 2.1720),
    "Bogatell Skatepark": (41.3944, 2.2043),
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
    """Build one seeded `Event` deterministically from its slug + offset.

    Start time is anchored to 19:00 UTC (21:00 CEST — evening) regardless
    of when the seed script runs. Using `now + timedelta(hours=19)` here
    would have made the wall-clock time depend on the seed run's time of
    day, breaking every "tonight" / "this evening" query.
    """
    base_date = (now + timedelta(days=days_from_now)).date()
    start = datetime.combine(base_date, time(19, 0), tzinfo=UTC)
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
    """The 60-event catalog — one deterministic list, all fields spelled out."""
    return [
        # ---- tech (10) ----
        _event(
            slug="ai-meetup-2026",
            title="Barcelona AI Builders Meetup",
            category="tech",
            days_from_now=0,  # tonight — see docstring "Same-day (`days_from_now=0`) picks"
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
            days_from_now=0,  # tonight
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
        _event(
            slug="python-bcn-meetup",
            title="Python Barcelona Monthly Meetup",
            category="tech",
            days_from_now=8,
            duration_hours=3,
            price_cents=0,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["python", "meetup"],
            description="Community meetup for Python developers — lightning talks, Q&A, pizza.",
        ),
        _event(
            slug="data-engineering-summit",
            title="Data Engineering Summit Barcelona",
            category="tech",
            days_from_now=16,
            duration_hours=6,
            price_cents=8500,
            venue="Fabra i Coats",
            now=now,
            tags=["data", "engineering", "conference"],
            description="One-day summit for data engineers — pipelines, warehouses, real-time.",
        ),
        _event(
            slug="cybersecurity-conf",
            title="Barcelona Cybersecurity Conference",
            category="tech",
            days_from_now=23,
            duration_hours=8,
            price_cents=15000,
            venue="CCCB",
            now=now,
            tags=["security", "conference"],
            description="Cybersecurity — red teaming, incident response, threat intel.",
        ),
        _event(
            slug="ml-ops-workshop",
            title="ML in Production — Hands-on Workshop",
            category="tech",
            days_from_now=6,
            duration_hours=5,
            price_cents=4500,
            venue="Antiga Fàbrica Estrella Damm",
            now=now,
            tags=["ml", "mlops", "workshop"],
            description="Hands-on: deploying and monitoring ML models at scale.",
        ),
        _event(
            slug="web3-builders-night",
            title="Web3 Builders Night",
            category="tech",
            days_from_now=26,
            duration_hours=3,
            price_cents=500,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["web3", "blockchain", "meetup"],
            description="Casual builder meetup — smart-contract demos, DeFi, on-chain analytics.",
        ),
        # ---- cultural (10) ----
        _event(
            slug="macba-exhibition-2026",
            title="Contemporary Art Exhibition at MACBA",
            category="cultural",
            days_from_now=0,  # tonight
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
        _event(
            slug="gaudi-walking-tour",
            title="Gaudi Architecture Walking Tour",
            category="cultural",
            days_from_now=3,
            duration_hours=3,
            price_cents=2000,
            venue="Sagrada Familia",
            now=now,
            tags=["gaudi", "architecture", "tour"],
            description="Gaudi masterpieces: Casa Batlló, La Pedrera, Sagrada Familia.",
        ),
        _event(
            slug="photo-exhibition-batllo",
            title="Photography Exhibition at Casa Batlló",
            category="cultural",
            days_from_now=9,
            duration_hours=4,
            price_cents=1500,
            venue="Casa Batlló",
            now=now,
            tags=["photography", "exhibition"],
            description="Contemporary photography — Catalan cities, documentary lens.",
        ),
        _event(
            slug="poetry-slam-born",
            title="Poetry Slam Night at El Born",
            category="cultural",
            days_from_now=15,
            duration_hours=2,
            price_cents=500,
            venue="El Born Cultural Centre",
            now=now,
            tags=["poetry", "spoken-word"],
            description="Bilingual poetry slam — CA, ES, EN open mic + featured poets.",
        ),
        _event(
            slug="documentary-screening-mnac",
            title="Documentary Film Screening at MNAC",
            category="cultural",
            days_from_now=19,
            duration_hours=3,
            price_cents=800,
            venue="MNAC",
            now=now,
            tags=["documentary", "film"],
            description="Documentary evening — social-impact films followed by a director Q&A.",
        ),
        _event(
            slug="literary-festival-rambla",
            title="Barcelona Literary Festival",
            category="cultural",
            days_from_now=26,
            duration_hours=6,
            price_cents=0,
            venue="La Rambla",
            now=now,
            tags=["literature", "festival", "free"],
            description="Open-air literary festival — readings, signings, book stalls.",
        ),
        # ---- music (10) ----
        _event(
            slug="apolo-techno-friday",
            title="Techno Friday at Sala Apolo",
            category="music",
            days_from_now=0,  # tonight
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
        _event(
            slug="jazz-apolo",
            title="Jazz Night at Sala Apolo",
            category="music",
            days_from_now=0,  # tonight — favourite target for /prefs demos
            duration_hours=4,
            price_cents=1800,
            venue="Sala Apolo",
            now=now,
            tags=["jazz", "live"],
            description="Live jazz trio — bebop, blues, and Catalan jazz standards.",
        ),
        _event(
            slug="flamenco-poble-espanyol",
            title="Traditional Flamenco Show",
            category="music",
            days_from_now=11,
            duration_hours=2,
            price_cents=2800,
            venue="Poble Espanyol",
            now=now,
            tags=["flamenco", "traditional"],
            description="Traditional flamenco performance — guitar, cante, and baile with tapas.",
        ),
        _event(
            slug="open-mic-born",
            title="Open Mic Night at El Born",
            category="music",
            days_from_now=13,
            duration_hours=3,
            price_cents=300,
            venue="El Born Cultural Centre",
            now=now,
            tags=["open-mic", "acoustic"],
            description="Weekly open mic — singer-songwriters and acoustic sets in a small room.",
        ),
        _event(
            slug="symphony-palau",
            title="Symphony Orchestra at Palau de la Musica",
            category="music",
            days_from_now=21,
            duration_hours=3,
            price_cents=4200,
            venue="Palau de la Musica",
            now=now,
            tags=["classical", "symphony", "orchestra"],
            description="Full-orchestra evening — Mahler and Falla in the Modernista concert hall.",
        ),
        _event(
            slug="reggaeton-razz",
            title="Reggaeton Saturday at Razzmatazz",
            category="music",
            days_from_now=27,
            duration_hours=6,
            price_cents=2000,
            venue="Razzmatazz",
            now=now,
            tags=["reggaeton", "club", "latin"],
            description="Latin club night — reggaeton, dembow, and Latin pop hits.",
        ),
        # ---- networking (10) ----
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
        _event(
            slug="product-managers-circle",
            title="Product Managers Circle",
            category="networking",
            days_from_now=5,
            duration_hours=2,
            price_cents=1500,
            venue="Pier 01 Barcelona Tech City",
            now=now,
            tags=["product", "management"],
            description="Product managers circle — one topic, small groups, shareouts.",
        ),
        _event(
            slug="data-scientists-meetup",
            title="Barcelona Data Scientists Meetup",
            category="networking",
            days_from_now=10,
            duration_hours=3,
            price_cents=0,
            venue="Antiga Fàbrica Estrella Damm",
            now=now,
            tags=["data-science", "meetup"],
            description="Data practitioners — deep-dive talk then open networking.",
        ),
        _event(
            slug="designers-coffee",
            title="Designers Coffee Chat",
            category="networking",
            days_from_now=13,
            duration_hours=2,
            price_cents=500,
            venue=None,
            now=now,
            tags=["design", "coffee"],
            description="Coffee-hour meetup for UX, product, and visual designers.",
        ),
        _event(
            slug="content-creators-bcn",
            title="Content Creators Barcelona",
            category="networking",
            days_from_now=17,
            duration_hours=2,
            price_cents=1000,
            venue="El Born Cultural Centre",
            now=now,
            tags=["content", "creators", "social"],
            description="Meetup for content creators — YouTubers, podcasters, newsletter writers.",
        ),
        _event(
            slug="sales-marketing-mixer",
            title="Sales & Marketing Mixer",
            category="networking",
            days_from_now=20,
            duration_hours=3,
            price_cents=1200,
            venue="Antiga Fàbrica Estrella Damm",
            now=now,
            tags=["sales", "marketing"],
            description="After-work mixer for B2B sales and marketing professionals.",
        ),
        _event(
            slug="international-students-welcome",
            title="International Students Welcome Night",
            category="networking",
            days_from_now=6,
            duration_hours=3,
            price_cents=500,
            venue="Poble Espanyol",
            now=now,
            tags=["students", "international", "welcome"],
            description="Welcome mixer for international students in Barcelona.",
        ),
        _event(
            slug="language-exchange",
            title="Language Exchange Tuesdays",
            category="networking",
            days_from_now=23,
            duration_hours=2,
            price_cents=0,
            venue=None,
            now=now,
            tags=["language", "exchange", "free"],
            description="Rotating groups — CA, ES, EN, and FR practice tables.",
        ),
        # ---- sports (10) ----
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
        _event(
            slug="beach-volleyball-tournament",
            title="Beach Volleyball Open Tournament",
            category="sports",
            days_from_now=7,
            duration_hours=6,
            price_cents=1000,
            venue="Barceloneta Beach",
            now=now,
            tags=["volleyball", "beach", "tournament"],
            description="Amateur beach volleyball tournament — 2v2 pools, all levels welcome.",
        ),
        _event(
            slug="basketball-pickup",
            title="Weekly Basketball Pickup Games",
            category="sports",
            days_from_now=0,  # tonight
            duration_hours=2,
            price_cents=0,
            venue="Parc de la Ciutadella",
            now=now,
            tags=["basketball", "pickup", "free"],
            description="Open pickup basketball — five-on-five rotations at the outdoor courts.",
        ),
        _event(
            slug="marathon-training-run",
            title="Barcelona Marathon Training Group Run",
            category="sports",
            days_from_now=10,
            duration_hours=2,
            price_cents=500,
            venue="Parc de la Ciutadella",
            now=now,
            tags=["running", "marathon", "training"],
            description="Structured long run with pacers — 15 km loop, coach-led warmups.",
        ),
        _event(
            slug="padel-open-play",
            title="Padel Club Open Play Night",
            category="sports",
            days_from_now=16,
            duration_hours=3,
            price_cents=1500,
            venue=None,
            now=now,
            tags=["padel", "racket"],
            description="Open padel play — rotating doubles matches, court fees included.",
        ),
        _event(
            slug="boxing-fitness-class",
            title="Boxing Fitness Class",
            category="sports",
            days_from_now=21,
            duration_hours=1,
            price_cents=1200,
            venue=None,
            now=now,
            tags=["boxing", "fitness"],
            description="Beginner-friendly boxing fitness — pads, bags, footwork drills.",
        ),
        _event(
            slug="rock-climbing-meetup",
            title="Indoor Rock Climbing Meetup",
            category="sports",
            days_from_now=26,
            duration_hours=3,
            price_cents=2000,
            venue=None,
            now=now,
            tags=["climbing", "bouldering", "indoor"],
            description="Bouldering + top-rope — all levels welcome.",
        ),
        # ---- other (10) ----
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
        _event(
            slug="vegan-food-fest",
            title="Vegan Food Festival",
            category="other",
            days_from_now=4,
            duration_hours=8,
            price_cents=500,
            venue="Poble Espanyol",
            now=now,
            tags=["food", "vegan", "festival"],
            description="Two-dozen vegan food stalls plus chef demos and a sustainability panel.",
        ),
        _event(
            slug="craft-beer-tasting",
            title="Craft Beer Tasting at Antiga Fàbrica",
            category="other",
            days_from_now=8,
            duration_hours=3,
            price_cents=2500,
            venue="Antiga Fàbrica Estrella Damm",
            now=now,
            tags=["beer", "tasting"],
            description="Ten Catalan breweries — guided flights + Q&A with brewers.",
        ),
        _event(
            slug="coffee-tasting",
            title="Specialty Coffee Cupping",
            category="other",
            days_from_now=14,
            duration_hours=2,
            price_cents=1500,
            venue=None,
            now=now,
            tags=["coffee", "cupping"],
            description="Coffee cupping — three origins, blind-taste protocol.",
        ),
        _event(
            slug="book-club-born",
            title="Book Club — Contemporary Fiction",
            category="other",
            days_from_now=18,
            duration_hours=2,
            price_cents=0,
            venue="El Born Cultural Centre",
            now=now,
            tags=["books", "reading", "free"],
            description="This month's pick — contemporary novel in ES / CA / EN.",
        ),
        _event(
            slug="board-games-night",
            title="Board Games Night",
            category="other",
            days_from_now=22,
            duration_hours=4,
            price_cents=500,
            venue=None,
            now=now,
            tags=["games", "board-games"],
            description="Casual board games night — bring your own or borrow from the shelf.",
        ),
        _event(
            slug="improv-comedy",
            title="Improv Comedy Night",
            category="other",
            days_from_now=25,
            duration_hours=2,
            price_cents=1500,
            venue="El Born Cultural Centre",
            now=now,
            tags=["comedy", "improv"],
            description="Bilingual improv comedy — audience prompts, short + long-form.",
        ),
        _event(
            slug="trivia-night",
            title="Trivia Night at the Pub",
            category="other",
            days_from_now=29,
            duration_hours=3,
            price_cents=500,
            venue=None,
            now=now,
            tags=["trivia", "pub"],
            description="Team trivia night — six rounds, mixed general knowledge, small prizes.",
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
