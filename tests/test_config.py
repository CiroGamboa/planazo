"""Environment loading and the two env-var guards.

The invariant two of these tests lock, in its two halves: `_env_path()`
resolves to the repository root's `.env`, and importing `planazo.config` on its
own — with no `agentlib` module in `sys.modules` — is what calls `load_dotenv`
with it. A surface that never touches the LLM wrapper still gets the repo
`.env`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from planazo.config import _env_path, check_api_key, read_bot_token

# Records what `load_dotenv` was called with, then reports it back as JSON.
# The spy is installed before `planazo.config` is imported, so the module's
# own `from dotenv import load_dotenv` binds the spy. Asserting on the call —
# not on `dotenv` being in `sys.modules` — is what makes the test fail if the
# `load_dotenv(_env_path())` line is ever dropped while the import stays.
_IMPORT_PROBE = """
import json
import sys

import dotenv

recorded = []
real = dotenv.load_dotenv


def spy(*args, **kwargs):
    recorded.append(args[0] if args else kwargs.get("dotenv_path"))
    return real(*args, **kwargs)


dotenv.load_dotenv = spy

import planazo.config

print(json.dumps({
    "recorded": [str(path) for path in recorded],
    "leaked": sorted(m for m in sys.modules if m == "agentlib" or m.startswith("agentlib.")),
}))
"""


def test_returns_true_and_prints_nothing_when_key_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "real-key")

    result = check_api_key()

    captured = capsys.readouterr()
    assert result is True
    assert captured.out == ""
    assert captured.err == ""


def test_returns_false_and_prints_message_when_key_is_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    result = check_api_key()

    captured = capsys.readouterr()
    assert result is False
    assert "OPENCODE_API_KEY" in captured.out
    assert ".env" in captured.out
    assert captured.err == ""


def test_env_path_is_the_repository_root_dotenv() -> None:
    """The whole discovery rule, asserted directly.

    Computed here from this test file's own location rather than by repeating
    `config.py`'s expression, so a change to either walk fails. No `.env` needs
    to exist: `load_dotenv` on a missing path returns False without raising,
    which is why CI is fine without one.
    """
    assert _env_path() == Path(__file__).resolve().parents[1] / ".env"


def test_importing_config_loads_the_repository_dotenv_on_its_own(tmp_path: Path) -> None:
    """`.env` reaches the process without `agentlib` being imported.

    Run from an unrelated working directory, because the anchor is what makes
    the resolution cwd-independent — a discovery walk would come back empty
    from here.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    reported = json.loads(completed.stdout)
    assert reported["recorded"] == [str(Path(__file__).resolve().parents[1] / ".env")]
    assert reported["leaked"] == []


def test_read_bot_token_returns_the_value_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")

    result = read_bot_token()

    captured = capsys.readouterr()
    assert result == "123:ABC"
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("value", [None, ""])
def test_read_bot_token_prints_and_returns_none_without_a_usable_token(
    value: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Empty and unset are one outcome: neither can start a bot, so neither is
    # allowed to reach the Bot API and fail there instead.
    if value is None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", value)

    result = read_bot_token()

    captured = capsys.readouterr()
    assert result is None
    assert "TELEGRAM_BOT_TOKEN" in captured.out
    assert ".env" in captured.out
    assert captured.err == ""


def test_returns_false_and_prints_message_when_key_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "")

    result = check_api_key()

    captured = capsys.readouterr()
    assert result is False
    assert "OPENCODE_API_KEY" in captured.out
    assert ".env" in captured.out
    assert captured.err == ""
