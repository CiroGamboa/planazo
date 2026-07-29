"""Contract tests for `planazo.observability.recommendations`.

Covers migration 005, `RecommendationRecord` construction discipline,
the `record_recommendations` + `query_recommendations` roundtrip, the
composite index the `/find` reader will use, and the end-to-end wiring
into `event_agent.run_once` — including the `record_runs=False` seam
that disables the writer alongside the other two observability
surfaces (JSONL trace, `agent_runs`, `llm_decisions`).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planazo.agents import event_agent
from planazo.agents.loop import LoopResult
from planazo.catalog.models import Event
from planazo.catalog.repository import insert_event
from planazo.identity import get_or_create_user
from planazo.memory import facts, rules
from planazo.observability import (
    RecommendationRecord,
    query_recommendations,
    record_recommendations,
)
from planazo.observability.models import AgentRunRecord
from planazo.observability.repository import record_agent_run
from planazo.query.models import SearchIntent
from planazo.storage import db

# ---- RecommendationRecord --------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_recommendation_record_constructs_from_valid_kwargs() -> None:
    """Every field flows through with the expected types."""
    record = RecommendationRecord(
        run_id="run-1",
        event_id=42,
        rank_position=0,
        score=0.87,
        reason="Matches your preferred category: tech.",
        recorded_at=_now(),
    )
    assert record.run_id == "run-1"
    assert record.event_id == 42
    assert record.rank_position == 0
    assert record.score == pytest.approx(0.87)
    assert record.reason == "Matches your preferred category: tech."


def test_recommendation_record_accepts_none_score_reason_event_id() -> None:
    """Today the ranker is not wired; nullable fields must accept `None`."""
    record = RecommendationRecord(
        run_id="run-1",
        event_id=None,
        rank_position=2,
        score=None,
        reason=None,
        recorded_at=_now(),
    )
    assert record.event_id is None
    assert record.score is None
    assert record.reason is None


def test_recommendation_record_rejects_unsanitized_reason() -> None:
    """A raw newline in `reason` trips the sanitizer regex at construction."""
    with pytest.raises(ValueError, match="reason must be sanitized"):
        RecommendationRecord(
            run_id="run-1",
            event_id=1,
            rank_position=0,
            score=0.5,
            reason="line1\nline2",
            recorded_at=_now(),
        )


def test_recommendation_record_rejects_reason_with_control_char() -> None:
    """DEL and every C0/C1 control byte are banned by the regex."""
    with pytest.raises(ValueError, match="reason must be sanitized"):
        RecommendationRecord(
            run_id="run-1",
            event_id=1,
            rank_position=0,
            score=0.5,
            reason=f"a{chr(0x7F)}b",
            recorded_at=_now(),
        )


def test_recommendation_record_rejects_reason_over_cap() -> None:
    """`max_length=500` is enforced at construction."""
    with pytest.raises(ValueError):
        RecommendationRecord(
            run_id="run-1",
            event_id=1,
            rank_position=0,
            score=0.5,
            reason="x" * 501,
            recorded_at=_now(),
        )


def test_recommendation_record_rejects_negative_rank_position() -> None:
    """`ge=0` — rank_position starts at 0 for the top-ranked candidate."""
    with pytest.raises(ValueError):
        RecommendationRecord(
            run_id="run-1",
            event_id=1,
            rank_position=-1,
            recorded_at=_now(),
        )


def test_recommendation_record_rejects_empty_run_id() -> None:
    """`min_length=1` — an empty run_id would break the FK."""
    with pytest.raises(ValueError):
        RecommendationRecord(
            run_id="",
            event_id=1,
            rank_position=0,
            recorded_at=_now(),
        )


# ---- Migration + repository ------------------------------------------------


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sqlite3.Connection:
    """A tmpdir-scoped DB brought forward through every migration + a seeded user + parent run."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    conn = db.connect()
    get_or_create_user(conn, "tg-1", "Test User")
    # Seed one `agent_runs` parent so the FK on `recommendations.run_id` resolves.
    record_agent_run(
        conn,
        AgentRunRecord(
            run_id="run-1",
            agent_kind="recommender",
            user_id=1,
            user_query="find music",
            final_answer="two candidates",
            stopped="answered",
            steps_count=2,
            started_at=_now(),
            ended_at=_now() + timedelta(seconds=2),
        ),
    )
    return conn


def _make_event(source_url: str, title: str) -> Event:
    now = _now()
    return Event(
        source="seed",
        source_url=source_url,
        title=title,
        start_utc=now + timedelta(days=1),
        end_utc=now + timedelta(days=1, hours=2),
        category="music",
        city="Barcelona",
        confidence=0.9,
    )


def test_migration_005_lands_recommendations_table(isolated_db: sqlite3.Connection) -> None:
    """`user_version` reaches 5 and the table + composite index exist."""
    version = int(isolated_db.execute("PRAGMA user_version").fetchone()[0])
    assert version >= 5

    tables = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "recommendations" in tables

    indexes = {
        row["name"]
        for row in isolated_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_recommendations_run_rank" in indexes


def test_record_and_query_roundtrip(isolated_db: sqlite3.Connection) -> None:
    """3 candidates in, 3 records out — indices 0/1/2 preserved."""
    events = [_make_event(f"seed://event/{i}", f"Event {i}") for i in range(3)]
    event_ids = [insert_event(isolated_db, e) for e in events]

    count = record_recommendations(
        isolated_db,
        run_id="run-1",
        ranked_events=[
            e.model_copy(update={"id": eid}) for e, eid in zip(events, event_ids, strict=True)
        ],
    )
    assert count == 3

    records = query_recommendations(isolated_db, run_id="run-1")
    assert [r.rank_position for r in records] == [0, 1, 2]
    assert [r.event_id for r in records] == event_ids
    # Today (M3.7 T1) score/reason land as NULL — the ranker is not wired yet.
    assert all(r.score is None for r in records)
    assert all(r.reason is None for r in records)


def test_record_recommendations_empty_sequence_is_noop(isolated_db: sqlite3.Connection) -> None:
    """An empty candidate list writes nothing (matches `no_results` branch)."""
    count = record_recommendations(isolated_db, run_id="run-1", ranked_events=[])
    assert count == 0
    assert query_recommendations(isolated_db, run_id="run-1") == []


def test_record_recommendations_rejects_orphan_run_id_via_fk(
    isolated_db: sqlite3.Connection,
) -> None:
    """FK enforcement: a `run_id` with no parent `agent_runs` row raises."""
    event = _make_event("seed://event/orphan", "Orphan")
    event_id = insert_event(isolated_db, event)
    with pytest.raises(sqlite3.IntegrityError):
        record_recommendations(
            isolated_db,
            run_id="run-does-not-exist",
            ranked_events=[event.model_copy(update={"id": event_id})],
        )


def test_event_id_on_delete_set_null(isolated_db: sqlite3.Connection) -> None:
    """Deleting an events row nulls out the pointer, preserves the row."""
    event = _make_event("seed://event/delete-me", "To Delete")
    event_id = insert_event(isolated_db, event)
    record_recommendations(
        isolated_db,
        run_id="run-1",
        ranked_events=[event.model_copy(update={"id": event_id})],
    )

    isolated_db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    isolated_db.commit()

    records = query_recommendations(isolated_db, run_id="run-1")
    assert len(records) == 1
    # Row survives the events delete; only the pointer nulls out.
    assert records[0].event_id is None


def test_query_uses_idx_recommendations_run_rank_for_run_and_rank_filter(
    isolated_db: sqlite3.Connection,
) -> None:
    """`EXPLAIN QUERY PLAN` for the WHERE-then-ORDER-BY names the composite index."""
    plan = isolated_db.execute(
        "EXPLAIN QUERY PLAN"
        " SELECT event_id FROM recommendations WHERE run_id = ?"
        " ORDER BY rank_position ASC LIMIT 100",
        ("run-1",),
    ).fetchall()
    plan_text = " ".join(str(row["detail"]) for row in plan)
    assert "idx_recommendations_run_rank" in plan_text, (
        f"query plan did not use idx_recommendations_run_rank: {plan_text!r}"
    )


def test_query_rejects_zero_limit(isolated_db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        query_recommendations(isolated_db, limit=0)


def test_query_skips_corrupt_row_with_warning(
    isolated_db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A raw-SQL row with unsanitized `reason` is dropped with a WARNING; healthy rows survive."""
    event = _make_event("seed://event/healthy", "Healthy")
    event_id = insert_event(isolated_db, event)
    record_recommendations(
        isolated_db,
        run_id="run-1",
        ranked_events=[event.model_copy(update={"id": event_id})],
    )
    # Inject a corrupt row via raw SQL that bypasses the Pydantic sanitizer:
    # raw newline in `reason` trips `_SANITIZED_TEXT_PATTERN` at re-validation.
    isolated_db.execute(
        "INSERT INTO recommendations"
        " (run_id, event_id, rank_position, score, reason, recorded_at)"
        " VALUES ('run-1', ?, 1, 0.5, 'bad\nreason', ?)",
        (event_id, _now().isoformat()),
    )
    isolated_db.commit()

    caplog.set_level(logging.WARNING, logger="planazo.observability.repository")

    records = query_recommendations(isolated_db, run_id="run-1")

    # Only the healthy row survives.
    assert len(records) == 1
    assert records[0].rank_position == 0

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.repository" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "run-1" in warnings[0].getMessage()
    assert "skipping row" in warnings[0].getMessage()


# ---- End-to-end wiring via event_agent.run_once ----------------------------


def _intent() -> SearchIntent:
    return SearchIntent(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        city="Barcelona",
        categories=("music",),
    )


@pytest.fixture
def isolated_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Rules dir, docstore, DB routed at a test tree — matches test_observability_end_to_end."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    monkeypatch.setattr(facts, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")
    monkeypatch.setattr(
        "planazo.extraction.audit.default_extraction_log_path",
        lambda: tmp_path / "extraction_runs.jsonl",
    )
    return tmp_path


def _seed_user() -> int:
    conn = db.connect()
    try:
        user = get_or_create_user(conn, "tg-1", "Test User")
        assert user.id is not None
        return user.id
    finally:
        conn.close()


def _seed_events_matching_intent(count: int) -> list[int]:
    """Seed `count` events matching `_intent()` so `_filter_candidates` retains them."""
    conn = db.connect()
    try:
        ids: list[int] = []
        for i in range(count):
            event = Event(
                source="seed",
                source_url=f"seed://event/e2e-{i}",
                title=f"Music Event {i}",
                start_utc=datetime(2026, 8, 1, 10 + i, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 1, 12 + i, 0, tzinfo=UTC),
                category="music",
                city="Barcelona",
                confidence=0.9,
            )
            ids.append(insert_event(conn, event))
        return ids
    finally:
        conn.close()


def _fake_search_result(event_rows: list[dict[str, object]]) -> dict[str, object]:
    return {"events": event_rows, "total": len(event_rows)}


def test_run_once_writes_one_recommendation_row_per_candidate(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: 3 candidates come back → 3 `recommendations` rows land with rank 0/1/2."""
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    # Seed 3 events matching the intent so `_filter_candidates` retains them
    # and the Recommender's post-loop projection produces 3 candidates.
    event_ids = _seed_events_matching_intent(3)

    # Mock `run_loop` so the LLM turn is skipped, but inject one
    # `search_events` observer call into `search_trace` so
    # `_build_recommender_result` finds candidates.
    def fake_run_loop(**kwargs: object) -> LoopResult:
        on_step = kwargs["on_step"]
        assert callable(on_step)
        from planazo.agents.loop import StepRecord

        # Read the seeded events back and hand them into the loop's step
        # trace exactly as the `search_events` tool would.
        conn = db.connect()
        try:
            rows = conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        events_json: list[dict[str, object]] = []
        for row in rows:
            from planazo.catalog.repository import _event_from_row  # local import — private

            e = _event_from_row(row)
            events_json.append(e.model_dump(mode="json"))
        on_step(
            StepRecord(
                step=1,
                tool="search_events",
                arguments={"category": "music", "city": "Barcelona"},
                result=_fake_search_result(events_json),
            )
        )
        return LoopResult(answer="here are three", steps=1, stopped="answered")

    monkeypatch.setattr(event_agent, "run_loop", fake_run_loop)

    result = event_agent.run_once(user_id, _intent())
    assert result.status == "ok"
    assert len(result.candidates) == 3

    conn = db.connect()
    try:
        records = query_recommendations(conn)
    finally:
        conn.close()
    assert len(records) == 3
    assert [r.rank_position for r in records] == [0, 1, 2]
    # `event_id` is populated from the candidate's own `id`, which came
    # through `Event.model_dump(mode="json")` above — every candidate
    # had a persisted id so the FK is satisfied.
    assert set(r.event_id for r in records) == set(event_ids)


def test_run_once_record_runs_false_disables_recommendation_writer(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_runs=False` disables the SQLite recommendations writer too."""
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")
    _seed_events_matching_intent(2)

    def fake_run_loop(**kwargs: object) -> LoopResult:
        on_step = kwargs["on_step"]
        assert callable(on_step)
        from planazo.agents.loop import StepRecord

        conn = db.connect()
        try:
            rows = conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        events_json: list[dict[str, object]] = []
        for row in rows:
            from planazo.catalog.repository import _event_from_row

            events_json.append(_event_from_row(row).model_dump(mode="json"))
        on_step(
            StepRecord(
                step=1,
                tool="search_events",
                arguments={"category": "music", "city": "Barcelona"},
                result=_fake_search_result(events_json),
            )
        )
        return LoopResult(answer="ok", steps=1, stopped="answered")

    monkeypatch.setattr(event_agent, "run_loop", fake_run_loop)

    result = event_agent.run_once(user_id, _intent(), record_runs=False)
    assert result.status == "ok"

    conn = db.connect()
    try:
        records = query_recommendations(conn)
    finally:
        conn.close()
    assert records == []


def test_run_once_recommendation_writer_failure_logs_warning_and_does_not_propagate(
    isolated_stores: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 4: a raise inside `RecommendationLogger.record` is swallowed with a WARNING."""
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")
    _seed_events_matching_intent(1)

    def fake_run_loop(**kwargs: object) -> LoopResult:
        on_step = kwargs["on_step"]
        assert callable(on_step)
        from planazo.agents.loop import StepRecord

        conn = db.connect()
        try:
            rows = conn.execute("SELECT * FROM events").fetchall()
        finally:
            conn.close()
        from planazo.catalog.repository import _event_from_row

        events_json = [_event_from_row(row).model_dump(mode="json") for row in rows]
        on_step(
            StepRecord(
                step=1,
                tool="search_events",
                arguments={},
                result=_fake_search_result(events_json),
            )
        )
        return LoopResult(answer="ok", steps=1, stopped="answered")

    monkeypatch.setattr(event_agent, "run_loop", fake_run_loop)

    # Make the recommendation writer's underlying primitive raise. The
    # writer swallows it and logs one WARNING; the Recommender's answer
    # and the RecommenderResult are unaffected.
    def _boom_record(*args: object, **kwargs: object) -> int:
        raise RuntimeError("simulated: recommendations write failed")

    monkeypatch.setattr("planazo.observability.logging.record_recommendations", _boom_record)

    caplog.set_level(logging.WARNING, logger="planazo.observability.logging")
    result = event_agent.run_once(user_id, _intent())
    assert result.status == "ok"

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "planazo.observability.logging" and rec.levelno == logging.WARNING
    ]
    assert any("recommendation_logger write failed" in w.getMessage() for w in warnings)


def test_run_once_no_results_writes_zero_recommendation_rows(
    isolated_stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `no_results` branch is a legal candidate list — writer no-ops on empty."""
    user_id = _seed_user()
    (isolated_stores / "rules" / "000-core-rules.md").write_text("RULES", encoding="utf-8")

    def fake_run_loop(**kwargs: object) -> LoopResult:
        on_step = kwargs["on_step"]
        assert callable(on_step)
        from planazo.agents.loop import StepRecord

        # Successful search that returned zero events — the "no_results"
        # branch of the Recommender's post-loop projection.
        on_step(
            StepRecord(
                step=1,
                tool="search_events",
                arguments={},
                result=_fake_search_result([]),
            )
        )
        return LoopResult(answer="nothing found", steps=1, stopped="answered")

    monkeypatch.setattr(event_agent, "run_loop", fake_run_loop)

    result = event_agent.run_once(user_id, _intent())
    assert result.status == "no_results"

    conn = db.connect()
    try:
        records = query_recommendations(conn)
    finally:
        conn.close()
    assert records == []
