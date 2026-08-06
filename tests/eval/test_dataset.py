"""Loader tests for the committed golden dataset + seed events corpus.

Read the real committed files under `data/eval/` and assert the coverage
invariants the retrieval eval hinges on: the seed corpus is at least 100
events, every seed row is a valid `Event`, the golden set is at least 20
cases with at least 5 distinct failure categories represented (including
one out-of-corpus case), and every `golden_event_id` cross-references a
real seed event id.

The loader itself is stateless — a malformed JSONL line raises a
`ValueError` at the boundary. The tests exercise that too via a temp
file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from planazo.catalog.models import Event
from planazo.eval.dataset import GoldenCase, load_golden_cases, load_seed_events

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "data" / "eval" / "events_seed.jsonl"
_GOLDEN_PATH = _REPO_ROOT / "data" / "eval" / "questions.jsonl"


def test_load_golden_cases_returns_all_rows() -> None:
    cases = load_golden_cases(_GOLDEN_PATH)

    assert len(cases) >= 20, f"expected >= 20 golden cases, got {len(cases)}"
    assert all(isinstance(case, GoldenCase) for case in cases)

    categories = {case.failure_category for case in cases}
    assert len(categories) >= 5, (
        f"expected >= 5 distinct failure categories represented, got {sorted(categories)}"
    )

    out_of_corpus = [case for case in cases if not case.golden_event_ids]
    assert out_of_corpus, "expected >= 1 out-of-corpus case (empty golden_event_ids)"
    assert all(case.failure_category == "out_of_corpus" for case in out_of_corpus), (
        "empty golden_event_ids must be tagged as out_of_corpus"
    )


def test_load_golden_cases_rejects_bad_row(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.jsonl"
    bad_file.write_text(
        '{"id": "q001", "query": "x", "golden_event_ids": [], '
        '"golden_answer": "y", "failure_category": "not_a_real_category"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid GoldenCase"):
        load_golden_cases(bad_file)


def test_load_seed_events_returns_valid_events() -> None:
    events = load_seed_events(_SEED_PATH)

    assert len(events) >= 100, f"expected >= 100 seed events, got {len(events)}"
    assert all(isinstance(event, Event) for event in events)
    assert all(event.id is not None for event in events)


def test_load_seed_events_cross_references_golden_ids() -> None:
    """Every golden_event_id must exist in events_seed.jsonl."""
    events = load_seed_events(_SEED_PATH)
    cases = load_golden_cases(_GOLDEN_PATH)

    seed_ids = {str(event.id) for event in events}
    missing: list[tuple[str, str]] = []
    for case in cases:
        for golden_id in case.golden_event_ids:
            if golden_id not in seed_ids:
                missing.append((case.id, golden_id))

    assert not missing, f"golden ids missing from seed corpus: {missing}"


def test_load_seed_events_rejects_missing_id(tmp_path: Path) -> None:
    """A seed row without an `id` breaks the chunk-id anchor invariant."""
    bad_file = tmp_path / "no_id.jsonl"
    bad_file.write_text(
        '{"source": "seed", "source_url": "seed://event/x", "title": "x", '
        '"start_utc": "2026-08-05T20:00:00+00:00", '
        '"end_utc": "2026-08-05T21:00:00+00:00", '
        '"category": "music", "city": "Barcelona", "confidence": 0.9}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="chunk-id"):
        load_seed_events(bad_file)
