"""Loaders for the committed RAG-eval fixture files.

`load_golden_cases` and `load_seed_events` read the two JSONL files under
`data/eval/` — the hand-authored golden query set and the seed events
corpus — validate every row through Pydantic, and return typed lists the
retrieval + generation harnesses consume.

Both loaders are strict: a malformed row raises immediately rather than
being skipped. This is fixture data committed by the maintainers, not
user input — a bad row is a bug to fix in the file, not a case to
silently drop.

Per [ADR 0025](../../../../docs/adr/0025-rag-over-events.md): the seed
corpus (~120 events) and the ≥20 golden cases live in `data/eval/` so the
evaluation harness is fully reproducible from a checkout of the repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planazo.catalog.models import Event

FailureCategory = Literal[
    "exact_term",
    "acronym",
    "lexical_semantic_mismatch",
    "near_duplicate_noise",
    "multi_hop",
    "out_of_corpus",
]


class GoldenCase(BaseModel):
    """One hand-authored golden query case.

    `golden_event_ids` is a list of `Event.id` values (rendered as strings
    so the field's shape matches the retriever's `Chunk.id` contract).
    An empty list is a valid, deliberate signal — the case belongs to the
    `out_of_corpus` failure category, where the correct behavior is "no
    events in the catalog for X." The retrieval scorers return `None` on
    empty-golden cases so the harness counts them separately.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    golden_event_ids: list[str] = Field(default_factory=list)
    golden_answer: str = Field(min_length=1)
    failure_category: FailureCategory


def load_golden_cases(path: Path) -> list[GoldenCase]:
    """Read `path` (a JSONL of `GoldenCase` rows) into a validated list.

    Raises `ValidationError` on any malformed row and `ValueError` on any
    line that isn't valid JSON.
    """
    cases: list[GoldenCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            try:
                cases.append(GoldenCase.model_validate(payload))
            except ValidationError as exc:
                raise ValueError(f"{path}:{line_number}: invalid GoldenCase") from exc
    return cases


def load_seed_events(path: Path) -> list[Event]:
    """Read `path` (a JSONL of `Event` rows) into a validated list.

    Every row is passed through `Event.model_validate` — the same boundary
    the runtime catalog uses. Rows missing a numeric `id` are rejected so
    the retriever's chunk-id invariant (chunk id == event id) holds.
    """
    events: list[Event] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            try:
                event = Event.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(f"{path}:{line_number}: invalid Event") from exc
            if event.id is None:
                raise ValueError(
                    f"{path}:{line_number}: seed event must carry an `id` (chunk-id anchor)"
                )
            events.append(event)
    return events
