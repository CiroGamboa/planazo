import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from planazo.monitor.models import Rationale, Verdict
from planazo.monitor.service import parse_since, repository_root, run_monitor


def _trace(run_id: str, *, agent: str, started_at: str, result: object) -> dict[str, object]:
    return {
        "run_id": run_id,
        "agent": agent,
        "started_at": started_at,
        "recorded_at": started_at,
        "model": "gpt-5.4-nano",
        "model_tier": "cheap",
        "user_message": "Find an event",
        "step": 1,
        "wall_clock_ms": 10,
        "tool_calls": [{"name": "search_events", "arguments": {}, "result": result}],
    }


def test_monitor_joins_runs_and_always_writes_real_problems_section(tmp_path: Path) -> None:
    recommender_dir = tmp_path / "data" / "runs"
    recommender_dir.mkdir(parents=True)
    extractor_log = tmp_path / "agent" / "var" / "extraction_runs.jsonl"
    extractor_log.parent.mkdir(parents=True)
    started_at = "2026-07-27T12:00:00Z"
    (recommender_dir / "run-1.jsonl").write_text(
        json.dumps(_trace("run-1", agent="recommender", started_at=started_at, result={})) + "\n",
        encoding="utf-8",
    )
    extractor_log.write_text(
        json.dumps(_trace("run-1", agent="extractor", started_at=started_at, result={})) + "\n",
        encoding="utf-8",
    )
    observed_step_counts: list[int] = []

    def judge(run: object) -> Verdict:
        observed_step_counts.append(len(run.steps))  # type: ignore[attr-defined]
        return Verdict(
            prompt_adherence="minor_violation",
            untrusted_content_handling="safe",
            rationale=Rationale(expected="match the request", actual="returned the wrong time"),
        )

    outputs = run_monitor(
        recommender_dir=recommender_dir,
        extractor_log=extractor_log,
        output_dir=tmp_path / "data" / "monitor",
        since=timedelta(hours=24),
        now=datetime(2026, 7, 27, 13, tzinfo=UTC),
        judge=judge,
    )

    report = (tmp_path / "data" / "monitor" / "2026-07-27.md").read_text(encoding="utf-8")
    sidecar = (tmp_path / "data" / "monitor" / "2026-07-27.jsonl").read_text(encoding="utf-8")
    assert observed_step_counts == [2]
    assert "## Real problems found" in report
    assert "returned the wrong time" in report
    assert '"run_id":"run-1"' in sidecar
    assert len(outputs) == 2


def test_parse_since_rejects_invalid_windows() -> None:
    assert parse_since("24h") == timedelta(hours=24)
    try:
        parse_since("tomorrow")
    except ValueError as exc:
        assert "--since" in str(exc)
    else:
        raise AssertionError("expected an invalid duration to fail")


def test_seed_sessions_are_independently_monitorable(tmp_path: Path) -> None:
    seed_dir = repository_root() / "scripts" / "monitor" / "seed_runs"

    def clean_judge(_run: object) -> Verdict:
        return Verdict(prompt_adherence="strictly_adheres", untrusted_content_handling="safe")

    outputs = run_monitor(
        recommender_dir=seed_dir / "runs",
        extractor_log=seed_dir / "extraction_runs.jsonl",
        output_dir=tmp_path,
        since=timedelta(weeks=9999),
        now=datetime(2026, 7, 27, 13, tzinfo=UTC),
        judge=clean_judge,
    )

    report = (tmp_path / "2026-07-27.md").read_text(encoding="utf-8")
    assert "Runs graded: 3" in report
    assert "## Real problems found\n\nNone." in report
    assert len(outputs) == 2


def test_run_monitor_can_select_one_seed_session(tmp_path: Path) -> None:
    seed_dir = repository_root() / "scripts" / "monitor" / "seed_runs"
    judged_run_ids: list[str] = []

    def clean_judge(run: object) -> Verdict:
        judged_run_ids.append(run.run_id)  # type: ignore[attr-defined]
        return Verdict(prompt_adherence="strictly_adheres", untrusted_content_handling="safe")

    run_monitor(
        recommender_dir=seed_dir / "runs",
        extractor_log=seed_dir / "extraction_runs.jsonl",
        output_dir=tmp_path,
        since=timedelta(weeks=9999),
        now=datetime(2026, 7, 27, 13, tzinfo=UTC),
        judge=clean_judge,
        run_ids={"seed-clean"},
    )

    assert judged_run_ids == ["seed-clean"]
