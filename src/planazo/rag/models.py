"""Pydantic v2 boundary models for the domain-agnostic RAG primitives.

Each retriever consumes `Chunk` objects and emits a `RetrievalResult` whose
`hits: list[Hit]` are already ranked. Every model is frozen + `extra="forbid"`
so a misplaced field trips at construction rather than silently coercing.

See [ADR 0025](../../../../docs/adr/0025-rag-over-events.md) for how these
shapes fit the wider `search_events` pipeline; the event-domain glue that
turns an `Event` into a `Chunk` lives in `planazo.catalog.rag`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Chunk(BaseModel):
    """One retrievable document — the atomic unit each index ingests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str = Field(min_length=1, max_length=20_000)


class Hit(BaseModel):
    """One ranked hit — a chunk id paired with the retriever's raw score + rank."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    score: float
    rank: int = Field(ge=1)


class RetrievalResult(BaseModel):
    """The full ranked list a retrieval stage emits, tagged with its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    hits: list[Hit]
    retriever: Literal["dense", "bm25", "rrf", "reranked"]
