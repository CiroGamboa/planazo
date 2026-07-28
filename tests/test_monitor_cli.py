from unittest.mock import MagicMock

import pytest

from planazo.monitor import cli
from planazo.monitor import logging as monitor_logging
from planazo.monitor.service import repository_root


def test_monitor_reports_a_missing_key_without_calling_the_judge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monitor = MagicMock()
    monkeypatch.setattr(cli, "run_monitor", monitor)

    code = cli.main(["--dry-run"])

    out = capsys.readouterr().out
    assert code == 1
    assert "OPENCODE_API_KEY" in out
    assert "Traceback" not in out
    monitor.assert_not_called()


def test_monitor_passes_an_optional_run_id_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monitor = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "run_monitor", monitor)

    code = cli.main(["--dry-run", "--run-id", "seed-clean"])

    assert code == 0
    assert monitor.call_args.kwargs["run_ids"] == {"seed-clean"}


def test_live_mode_extractor_log_lives_at_repo_var_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-mode branch of `monitor.cli.main` builds
    `extractor_log = <repo_root>/var/extraction_runs.jsonl` — no `agent/`
    segment (post-M9 flatten)."""
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monitor = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "run_monitor", monitor)

    code = cli.main([])

    assert code == 0
    extractor_log = monitor.call_args.kwargs["extractor_log"]
    assert extractor_log == repository_root() / "var" / "extraction_runs.jsonl"
    assert "agent" not in extractor_log.parts


def test_default_run_log_dir_points_at_repo_data_runs() -> None:
    """`monitor.logging.default_run_log_dir` walks the right number of
    parents up — the wrong number silently points one directory above the
    repo root and every default-path caller would write outside the tree."""
    default = monitor_logging.default_run_log_dir()
    assert default == repository_root() / "data" / "runs"
