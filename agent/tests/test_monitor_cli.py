from unittest.mock import MagicMock

import pytest

from planazo.monitor import cli


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
