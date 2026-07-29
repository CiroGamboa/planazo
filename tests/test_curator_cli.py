"""`planazo-curator` CLI — argparse contract + exit-code taxonomy + narrative log.

Every test monkeypatches `run_curator` so no real LLM fires. Coverage:

- `--tick` is required.
- Happy-path exit 0 + one-line summary to stdout.
- `--verbose` prints one line per StepRecord + terminal line.
- `--dry-run` propagates as `dry_run=True` into `run_curator`.
- Uncaught exception maps to exit 1 with a stderr line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from planazo.agents.loop import LoopResult, StepRecord
from planazo.curator import cli as curator_cli
from planazo.curator.agent import CuratorRunResult


def _tick_result(**overrides: Any) -> CuratorRunResult:
    defaults: dict[str, Any] = {
        "run_id": "abcdef01-run-id",
        "stopped": "answered",
        "steps": 3,
        "events_examined": 5,
        "events_archived": 2,
        "events_merged": 1,
        "categories_updated": 1,
        "errors": [],
        "dry_run": False,
        "started_at": datetime(2026, 7, 29, 3, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 7, 29, 3, 0, 30, tzinfo=UTC),
    }
    defaults.update(overrides)
    return CuratorRunResult(**defaults)


def _install_run_curator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tick_result: CuratorRunResult | None = None,
    exc: Exception | None = None,
    scripted_trace: list[StepRecord] | None = None,
    loop_stopped: str = "answered",
    loop_steps: int = 3,
) -> dict[str, Any]:
    """Replace `curator.cli.run_curator` with a scripted stub.

    Returns a dict the test can inspect for `dry_run`, `audit_log_path`, and
    the actual `on_step` / `on_complete` callables the CLI plumbed in.
    """
    captured: dict[str, Any] = {}

    def fake_run_curator(**kwargs: Any) -> CuratorRunResult:
        captured.update(kwargs)
        if exc is not None:
            raise exc
        on_step = kwargs.get("on_step")
        on_complete = kwargs.get("on_complete")
        if on_step is not None and scripted_trace is not None:
            for record in scripted_trace:
                on_step(record)
        if on_complete is not None:
            on_complete(LoopResult(answer="ok", steps=loop_steps, stopped=loop_stopped))  # type: ignore[arg-type]
        return tick_result if tick_result is not None else _tick_result()

    monkeypatch.setattr(curator_cli, "run_curator", fake_run_curator)
    return captured


def test_tick_flag_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        curator_cli.main([])

    assert info.value.code != 0


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        curator_cli.main(["--help"])

    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--tick" in out
    assert "--dry-run" in out
    assert "--verbose" in out


def test_tick_returns_zero_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_run_curator(monkeypatch)

    code = curator_cli.main(["--tick"])

    assert code == 0
    out = capsys.readouterr().out
    assert "tick: run_id=abcdef01" in out
    assert "stopped=answered" in out
    assert "archived=2" in out
    assert "merged=1" in out
    assert "updated=1" in out
    assert "dry_run=False" in out


def test_dry_run_flag_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_run_curator(monkeypatch, tick_result=_tick_result(dry_run=True))

    code = curator_cli.main(["--tick", "--dry-run"])

    assert code == 0
    assert captured["dry_run"] is True


def test_default_dry_run_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_run_curator(monkeypatch)

    curator_cli.main(["--tick"])

    assert captured["dry_run"] is False


def test_verbose_wires_narrative_callbacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = [
        StepRecord(
            step=1,
            tool="list_stale_events",
            arguments={"limit": 50},
            result={"total": 3, "events": []},
        ),
        StepRecord(
            step=2,
            tool="archive_event",
            arguments={"event_id": 17, "reason": "past"},
            result={"status": "ok", "archived_event_id": 17},
        ),
        StepRecord(
            step=3,
            tool="merge_events",
            arguments={"keep_event_id": 32, "archive_event_ids": [33], "reason": "dupe"},
            result={"status": "ok", "kept_event_id": 32, "archived_event_ids": [33]},
        ),
        StepRecord(
            step=4,
            tool="update_event_category",
            arguments={"event_id": 41, "new_category": "cultural", "reason": "fix"},
            result={"status": "ok", "event_id": 41},
        ),
    ]
    _install_run_curator(monkeypatch, scripted_trace=trace, loop_steps=4)

    code = curator_cli.main(["--tick", "--verbose"])

    assert code == 0
    out = capsys.readouterr().out
    assert "list_stale_events" in out
    assert "3 row(s)" in out
    assert "archive_event(event_id=17)" in out
    assert "merge_events(keep=32, archive=1 id(s))" in out
    assert "update_event_category(event_id=41, new_category='cultural')" in out
    assert "loop terminal: stopped=answered" in out


def test_verbose_off_produces_no_step_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = [
        StepRecord(
            step=1,
            tool="archive_event",
            arguments={"event_id": 17, "reason": "past"},
            result={"status": "ok", "archived_event_id": 17},
        ),
    ]
    captured = _install_run_curator(monkeypatch, scripted_trace=trace)

    curator_cli.main(["--tick"])

    # `on_step` / `on_complete` should be None when --verbose is absent.
    assert captured["on_step"] is None
    assert captured["on_complete"] is None
    out = capsys.readouterr().out
    assert "[01]" not in out  # no narrative prefix


def test_uncaught_exception_maps_to_exit_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_run_curator(monkeypatch, exc=RuntimeError("simulated blow-up"))

    code = curator_cli.main(["--tick"])

    assert code == 1
    err = capsys.readouterr().err
    assert "planazo-curator: uncaught RuntimeError" in err
    assert "simulated blow-up" in err


def test_exit_one_truncates_long_exception_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    long_message = "x" * 500
    _install_run_curator(monkeypatch, exc=RuntimeError(long_message))

    curator_cli.main(["--tick"])

    err_line = capsys.readouterr().err.strip()
    # 120-char detail limit + prefix — line stays well under 500.
    assert len(err_line) < 300


def test_audit_log_path_is_wired_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from planazo.curator.models import DEFAULT_AUDIT_LOG_PATH

    captured = _install_run_curator(monkeypatch)

    curator_cli.main(["--tick"])

    assert captured["audit_log_path"] == DEFAULT_AUDIT_LOG_PATH
    assert isinstance(captured["audit_log_path"], Path)
