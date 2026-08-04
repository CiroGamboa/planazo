"""Unit tests for the event-domain RAG glue.

Covers the deterministic event-to-document projection, the chunk-building
invariants (ids preserved, order preserved, missing ids rejected), and the
`search_events_rag` orchestrator's contract — the seam ADR 0025 threads
through so the eval harness can toggle the reranker in isolation.

The fixture loads real sentence-transformer models the first time it runs
under a fresh process (~90 MB dense + ~90 MB cross-encoder cached in
`~/.cache/huggingface/`); subsequent tests amortize the load via module
scope.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from planazo.catalog import Event
from planazo.catalog.rag import (
    build_event_chunks,
    event_to_document,
    search_events_rag,
)


def _event(event_id: int, **overrides: object) -> Event:
    values: dict[str, object] = {
        "id": event_id,
        "source": "seed",
        "source_url": f"https://seed.example/e/{event_id}",
        "title": "Untitled",
        "start_utc": datetime(2026, 8, 1, 19, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 21, tzinfo=UTC),
        "category": "music",
        "city": "Barcelona",
        "confidence": 0.9,
        "event_index_in_post": event_id,
    }
    values.update(overrides)
    return Event(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def sample_events() -> list[Event]:
    return [
        _event(
            1,
            title="Flamenco night at Palau Dalmases",
            category="music",
            city="Barcelona",
            venue_name="Palau Dalmases",
            venue_address="Carrer de Montcada 20",
            tags=["flamenco", "live-music"],
            description="Intimate flamenco performance in a Gothic Quarter palace.",
            price_cents=2500,
        ),
        _event(
            2,
            title="Jazz jam at Jamboree",
            category="music",
            city="Barcelona",
            venue_name="Jamboree",
            venue_address="Plaça Reial 17",
            tags=["jazz", "jam-session"],
            description="Weekly jam session with resident quartet and open mic.",
            price_cents=1500,
        ),
        _event(
            3,
            title="Techno all-nighter at Sala Apolo",
            category="music",
            city="Barcelona",
            venue_name="Sala Apolo",
            venue_address="Nou de la Rambla 113",
            tags=["techno", "dj-set"],
            description="Marathon techno night with three headliners.",
            price_cents=1800,
        ),
        _event(
            4,
            title="AI + startups meetup",
            category="tech",
            city="Barcelona",
            venue_name="OneCoWork",
            venue_address="Passeig de Gràcia 5",
            tags=["ai", "startups"],
            description="Talks on production LLM systems and Q&A with founders.",
            price_cents=0,
        ),
        _event(
            5,
            title="Rooftop tapas & vermut",
            category="cultural",
            city="Barcelona",
            venue_name="El Nacional",
            venue_address="Passeig de Gràcia 24",
            tags=["tapas", "rooftop"],
            description="Golden-hour rooftop terrace with tapas and vermut pairings.",
            price_cents=3500,
        ),
        _event(
            6,
            title="Sunday football at Camp Nou",
            category="sports",
            city="Barcelona",
            venue_name="Camp Nou",
            venue_address="Carrer d'Aristides Maillol 12",
            tags=["football", "match"],
            description="FC Barcelona home match — league fixture.",
            price_cents=6000,
        ),
    ]


def test_event_to_document_deterministic(sample_events: list[Event]) -> None:
    """The projection is a pure function of `Event` fields."""
    event = sample_events[0]
    assert event_to_document(event) == event_to_document(event)


def test_event_to_document_handles_missing_optional_fields() -> None:
    """`None`/empty optional fields collapse to empty text without crashing."""
    event = _event(
        99,
        title="Bare event",
        description=None,
        venue_name=None,
        venue_address=None,
        tags=[],
        price_cents=0,
    )
    document = event_to_document(event)

    assert "Bare event" in document
    assert "Venue:  at" in document  # both optional venue fields collapse to ""
    assert "Tags: " in document
    assert "Price: free" in document


def test_event_to_document_renders_paid_price_as_cents(sample_events: list[Event]) -> None:
    document = event_to_document(sample_events[0])  # 2500 cents
    assert "Price: 2500" in document


def test_build_event_chunks_preserves_order(sample_events: list[Event]) -> None:
    subset = sample_events[:4]
    chunks = build_event_chunks(subset)

    assert [chunk.id for chunk in chunks] == [str(event.id) for event in subset]
    assert len(chunks) == 4


def test_build_event_chunks_rejects_event_without_id() -> None:
    orphan = _event(1)
    orphan_no_id = orphan.model_copy(update={"id": None})

    with pytest.raises(ValueError, match="id"):
        build_event_chunks([orphan_no_id])


def test_search_events_rag_returns_top_k_events(sample_events: list[Event]) -> None:
    """A concrete flamenco query surfaces the flamenco event on top."""
    results = search_events_rag(sample_events, "traditional flamenco show tonight", k=3)

    assert 1 <= len(results) <= 3
    assert results[0].id == 1  # the flamenco event


def test_search_events_rag_rerank_toggle(sample_events: list[Event]) -> None:
    """`rerank=True`/`rerank=False` both return `list[Event]`; both surface the golden id."""
    with_rerank = search_events_rag(sample_events, "jazz jam session", rerank=True, k=3)
    without_rerank = search_events_rag(sample_events, "jazz jam session", rerank=False, k=3)

    assert with_rerank and without_rerank
    assert all(isinstance(event, Event) for event in with_rerank)
    assert all(isinstance(event, Event) for event in without_rerank)
    ids_rerank = [event.id for event in with_rerank]
    ids_no_rerank = [event.id for event in without_rerank]
    assert 2 in ids_rerank  # the jazz-jam event
    assert 2 in ids_no_rerank


def test_search_events_rag_empty_events_returns_empty() -> None:
    assert search_events_rag([], "flamenco tonight") == []


@pytest.mark.parametrize("query", ["", "   "])
def test_search_events_rag_empty_query_returns_empty(
    sample_events: list[Event], query: str
) -> None:
    assert search_events_rag(sample_events, query) == []
