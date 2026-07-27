import pytest

from planazo.config import check_api_key


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
