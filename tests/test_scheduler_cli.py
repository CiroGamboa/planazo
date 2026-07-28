"""Unit tests for `planazo.scheduler.cli` — the `planazo-scheduler` entry point.

Covers:

- argparse contract: `--tick`/`--once` mutually exclusive, exactly one required.
- Exit-code taxonomy (M7 of the plan):
  - `0` on any completed tick (including "every URL failed typed").
  - `1` on an uncaught exception escaping `run_tick`.
  - `2` on config-validation failure, missing sources.yaml, or missing HikerAPI keys.
- `--once <post_url>` bypasses cadence via `_process_source_url(bypass_cadence_gate=True)`
  while still writing an audit record — the M8 handoff between `--once` and `--tick`.
- `--once <reel_url>` — same path via the reel-shape URL.
- `--once <account_url>` — configured accounts route through the backend from
  `sources.yaml`; unconfigured accounts exit `2` with a typed error line.

Every test monkeypatches `_load_config_for_cli`, `_build_extractor`, and
`_build_backends` to inject fakes. No real LLM, no real HikerAPI, no real Meta,
no real DB file — the DB fixture points at `tmp_path` via
`monkeypatch.setattr(db, "DB_PATH", tmp_file)`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from planazo.catalog.models import Event
from planazo.extraction.models import ExtractionResult
from planazo.scheduler import cli as scheduler_cli
from planazo.scheduler.models import SchedulerRunRecord, TickReport
from planazo.scheduler.service import ExtractorCallable
from planazo.sources.config import (
    AccountConfig,
    MediaTypeFlags,
    PostConfig,
    SourceConfig,
    SourcesConfig,
)
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol
from planazo.storage import db

# ---- constants ------------------------------------------------------------

POST_URL = "https://www.instagram.com/p/AAAAAA/"
REEL_URL = "https://www.instagram.com/reel/BBBBBB/"
ACCOUNT_URL = "https://www.instagram.com/curated.agenda/"
UNCONFIGURED_ACCOUNT_URL = "https://www.instagram.com/someone_else/"

FIXED_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


# ---- fixtures + helpers ---------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point `db.DB_PATH` at a per-test tmp file so `_conn_factory` writes there."""
    db_file = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    yield db_file


@pytest.fixture
def audit_log_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect `DEFAULT_AUDIT_LOG_PATH` into `tmp_path` for CLI writes."""
    path = tmp_path / "scheduler_runs.jsonl"
    monkeypatch.setattr(scheduler_cli, "DEFAULT_AUDIT_LOG_PATH", path)
    return path


def _source_config(
    *,
    posts: list[PostConfig] | None = None,
    accounts: list[AccountConfig] | None = None,
    default_cadence: timedelta = timedelta(hours=6),
) -> SourceConfig:
    return SourceConfig(
        default_cadence=default_cadence,
        default_media_types=MediaTypeFlags(),
        posts=posts or [],
        accounts=accounts or [],
    )


def _config_with(source: SourceConfig) -> SourcesConfig:
    return SourcesConfig(sources={"instagram": source})


def _ok_result(url: str) -> ExtractionResult:
    event = Event(
        source="instagram",
        source_url=url,
        title="event",
        start_utc=FIXED_NOW,
        end_utc=FIXED_NOW,
        category="music",
        city="Barcelona",
        confidence=0.9,
    )
    return ExtractionResult(status="ok", events=[event], notes="")


def _typed_error_result() -> ExtractionResult:
    return ExtractionResult(status="error", error_type="not_found", notes="not found")


class _CountingExtractor:
    """Records every call; returns a scripted per-URL response."""

    def __init__(self, script: dict[str, ExtractionResult] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.script = script or {}

    def __call__(self, url: str, delegator_user_id: int) -> ExtractionResult:
        self.calls.append((url, delegator_user_id))
        return self.script.get(url, _ok_result(url))


class _ScriptedBackend:
    """Discovery-protocol stub returning a canned list."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.calls: list[tuple[str, int]] = []

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        self.calls.append((account_url, limit))
        return list(self.urls)


class _ExplodingBackend:
    """Asserts if called — used to prove routing bypasses the wrong slot."""

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        raise AssertionError(f"unexpected discovery call: {account_url!r}, limit={limit}")


def _install_backends(
    monkeypatch: pytest.MonkeyPatch,
    *,
    anonymous: InstagramDiscoveryProtocol | None = None,
    hikerapi: InstagramDiscoveryProtocol | None = None,
) -> dict[str, InstagramDiscoveryProtocol]:
    """Monkeypatch `_build_backends` to return the injected stubs."""
    backends: dict[str, InstagramDiscoveryProtocol] = {
        "anonymous": anonymous or _ExplodingBackend(),
        "hikerapi": hikerapi or _ExplodingBackend(),
    }
    monkeypatch.setattr(scheduler_cli, "_build_backends", lambda config: backends)
    return backends


def _install_config(monkeypatch: pytest.MonkeyPatch, config: SourcesConfig) -> None:
    monkeypatch.setattr(scheduler_cli, "_load_config_for_cli", lambda: config)


def _install_extractor(monkeypatch: pytest.MonkeyPatch, extractor: ExtractorCallable) -> None:
    monkeypatch.setattr(scheduler_cli, "_build_extractor", lambda: extractor)


# ---- argparse contract ----------------------------------------------------


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """`planazo-scheduler --help` renders argparse's help and exits 0.

    Argparse raises `SystemExit(0)` on `--help`; we assert the shape so a
    future regression that swallows the help output surfaces.
    """
    with pytest.raises(SystemExit) as info:
        scheduler_cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--tick" in out
    assert "--once" in out


def test_no_flag_exits_non_zero() -> None:
    """Neither `--tick` nor `--once` → argparse `SystemExit(2)`."""
    with pytest.raises(SystemExit) as info:
        scheduler_cli.main([])
    assert info.value.code != 0


def test_tick_and_once_are_mutually_exclusive() -> None:
    """`--tick` and `--once <url>` cannot be combined."""
    with pytest.raises(SystemExit) as info:
        scheduler_cli.main(["--tick", "--once", POST_URL])
    assert info.value.code != 0


# ---- --tick exit-code taxonomy (M7) ---------------------------------------


def test_tick_exits_0_on_successful_run(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """A tick that extracts one event returns exit code 0."""
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL)]))
    _install_config(monkeypatch, config)
    _install_backends(monkeypatch)
    _install_extractor(monkeypatch, _CountingExtractor())

    assert scheduler_cli.main(["--tick"]) == 0
    lines = audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_tick_exits_0_when_all_urls_fail_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """Every configured URL fails with a typed error — tick still exits 0.

    Locks M7: operators read `var/scheduler_runs.jsonl` for per-URL health;
    cron treats non-zero as "the tick itself blew up". A failed extraction
    is expected in steady state and does NOT bump the exit code.
    """
    config = _config_with(
        _source_config(
            posts=[PostConfig(url=POST_URL), PostConfig(url=REEL_URL)],
        )
    )
    _install_config(monkeypatch, config)
    _install_backends(monkeypatch)
    _install_extractor(
        monkeypatch,
        _CountingExtractor(
            script={POST_URL: _typed_error_result(), REEL_URL: _typed_error_result()}
        ),
    )

    assert scheduler_cli.main(["--tick"]) == 0

    lines = audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        record = SchedulerRunRecord.model_validate_json(line)
        assert record.posts_extracted_ok == 0
        assert record.posts_extracted_error == 1


def test_tick_exits_1_on_uncaught_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Path,
    audit_log_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An `Exception` escaping `run_tick` maps to exit code 1.

    Also asserts the one-liner to stderr carries the exception class name
    and no more than `_EXCEPTION_MESSAGE_TRUNCATE` chars of detail — Rule 2
    discipline extends to the exception-hoist path.
    """
    config = _config_with(_source_config())
    _install_config(monkeypatch, config)
    _install_backends(monkeypatch)

    def _boom(**kwargs: Any) -> TickReport:
        raise OSError("db corruption while writing " + ("x" * 500))

    monkeypatch.setattr(scheduler_cli, "run_tick", _boom)

    assert scheduler_cli.main(["--tick"]) == 1
    err = capsys.readouterr().err
    assert "planazo-scheduler: uncaught OSError:" in err
    # The truncated detail must be ≤ 120 chars; the whole stderr line is a bit
    # longer because of the "planazo-scheduler: uncaught ..." prefix.
    stderr_line = err.strip().splitlines()[-1]
    detail = stderr_line.split(": ", 2)[-1]
    assert len(detail) <= 120
    assert "\n" not in detail


def test_tick_exits_2_on_config_validation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed `sources.yaml` surfaces as `ValidationError` → exit code 2."""

    def _boom() -> SourcesConfig:
        # Trigger a real ValidationError so `main`'s except-clause has real
        # bytes to truncate — matches the shape production code sees.
        SourcesConfig.model_validate({"sources": {"instagram": {"nonsense": True}}})
        raise AssertionError("model_validate should have raised")  # pragma: no cover

    monkeypatch.setattr(scheduler_cli, "_load_config_for_cli", _boom)

    assert scheduler_cli.main(["--tick"]) == 2
    err = capsys.readouterr().err
    assert "planazo-scheduler: config error" in err


def test_tick_exits_2_on_missing_sources_yaml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing `data/sources.yaml` surfaces as `FileNotFoundError` → exit 2."""

    def _boom() -> SourcesConfig:
        raise FileNotFoundError(2, "No such file or directory", "data/sources.yaml")

    monkeypatch.setattr(scheduler_cli, "_load_config_for_cli", _boom)

    assert scheduler_cli.main(["--tick"]) == 2
    err = capsys.readouterr().err
    assert "planazo-scheduler: config error" in err


def test_tick_exits_2_when_hikerclient_from_env_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Path,
    audit_log_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `PLANAZO_IG_HIKER_API_KEY_*` set + a `hikerapi` account → exit 2.

    Config-time failure surface: `_build_backends` calls
    `HikerClient.from_env()`, which raises `RuntimeError` on an empty pool.
    """
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="hikerapi")])
    )
    _install_config(monkeypatch, config)

    def _boom(config_arg: SourcesConfig) -> dict[str, Any]:
        raise RuntimeError(
            "no PLANAZO_IG_HIKER_API_KEY_* env vars set — set at least one and re-run"
        )

    monkeypatch.setattr(scheduler_cli, "_build_backends", _boom)

    assert scheduler_cli.main(["--tick"]) == 2
    err = capsys.readouterr().err
    assert "planazo-scheduler: config error" in err


# ---- --tick composition wiring --------------------------------------------


def test_tick_invokes_run_tick_with_composed_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """`--tick` calls `run_tick` with `now`, `conn_factory`, backends, and extractor."""
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL)]))
    _install_config(monkeypatch, config)
    installed_backends = _install_backends(monkeypatch)
    extractor = _CountingExtractor()
    _install_extractor(monkeypatch, extractor)

    captured: dict[str, Any] = {}

    def _spy_run_tick(**kwargs: Any) -> TickReport:
        captured.update(kwargs)
        return TickReport(records=[], total_events_extracted=0, wall_clock_ms=0)

    monkeypatch.setattr(scheduler_cli, "run_tick", _spy_run_tick)

    assert scheduler_cli.main(["--tick"]) == 0
    assert captured["config"] is config
    assert captured["backends"] is installed_backends
    assert captured["extractor"] is extractor
    assert captured["audit_log_path"] == audit_log_path
    # `now` and `conn_factory` are callables; assert shape, not identity.
    assert callable(captured["now"])
    assert callable(captured["conn_factory"])


# ---- --once <post_url> ----------------------------------------------------


def test_once_with_post_url_invokes_process_source_url_with_bypass_cadence_gate_true(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """`--once <post_url>` invokes `_process_source_url(bypass_cadence_gate=True)`.

    M8 handoff at the CLI seam: the shared work unit sees `bypass_cadence_gate=True`
    from `--once` and `False` from `--tick`; every other seam is identical.
    """
    _install_config(monkeypatch, _config_with(_source_config()))
    _install_backends(monkeypatch)
    _install_extractor(monkeypatch, _CountingExtractor())

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> SchedulerRunRecord:
        captured.update(kwargs)
        return SchedulerRunRecord(
            run_id="fake",
            source_url=kwargs["source_url"],
            source_kind=kwargs["source_kind"],
            backend=kwargs["backend_name"],
            gate_reason="first_run",
            posts_discovered=0,
            posts_extracted_ok=1,
            posts_extracted_error=0,
            posts_skipped_idempotent=0,
            started_at=FIXED_NOW,
            ended_at=FIXED_NOW,
        )

    monkeypatch.setattr(scheduler_cli, "_process_source_url", _spy)

    assert scheduler_cli.main(["--once", POST_URL]) == 0
    assert captured["bypass_cadence_gate"] is True
    assert captured["source_kind"] == "post"
    assert captured["source_url"] == POST_URL
    assert captured["backend_client"] is None
    assert captured["backend_name"] is None


def test_once_with_reel_url_invokes_process_source_url_with_bypass_cadence_gate_true(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """`--once <reel_url>` follows the same post-URL branch as `/p/`."""
    _install_config(monkeypatch, _config_with(_source_config()))
    _install_backends(monkeypatch)
    _install_extractor(monkeypatch, _CountingExtractor())

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> SchedulerRunRecord:
        captured.update(kwargs)
        return SchedulerRunRecord(
            run_id="fake",
            source_url=kwargs["source_url"],
            source_kind=kwargs["source_kind"],
            backend=kwargs["backend_name"],
            gate_reason="first_run",
            posts_discovered=0,
            posts_extracted_ok=1,
            posts_extracted_error=0,
            posts_skipped_idempotent=0,
            started_at=FIXED_NOW,
            ended_at=FIXED_NOW,
        )

    monkeypatch.setattr(scheduler_cli, "_process_source_url", _spy)

    assert scheduler_cli.main(["--once", REEL_URL]) == 0
    assert captured["bypass_cadence_gate"] is True
    assert captured["source_kind"] == "post"
    assert captured["source_url"] == REEL_URL


def test_once_post_url_bypasses_cadence_gate_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """Integration through the shared helper: the extractor is invoked for the URL.

    Complementary to the mocked-`_process_source_url` tests above — proves the
    CLI actually reaches the extractor for a post URL without any cadence
    pre-seeded state to relax.
    """
    _install_config(monkeypatch, _config_with(_source_config()))
    _install_backends(monkeypatch)
    extractor = _CountingExtractor()
    _install_extractor(monkeypatch, extractor)

    assert scheduler_cli.main(["--once", POST_URL]) == 0
    assert len(extractor.calls) == 1
    called_url, uid = extractor.calls[0]
    assert called_url == POST_URL
    assert uid > 0

    line = audit_log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    record = SchedulerRunRecord.model_validate_json(line)
    assert record.source_url == POST_URL
    assert record.source_kind == "post"
    assert record.posts_extracted_ok == 1


# ---- --once <account_url> -------------------------------------------------


def test_once_with_configured_account_url_routes_to_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """A configured `hikerapi` account routes through the hikerapi backend."""
    account = AccountConfig(url=ACCOUNT_URL, backend="hikerapi")
    config = _config_with(_source_config(accounts=[account]))
    _install_config(monkeypatch, config)

    hikerapi_backend = _ScriptedBackend(urls=[POST_URL])
    _install_backends(monkeypatch, hikerapi=hikerapi_backend)
    extractor = _CountingExtractor()
    _install_extractor(monkeypatch, extractor)

    assert scheduler_cli.main(["--once", ACCOUNT_URL]) == 0
    assert hikerapi_backend.calls == [(ACCOUNT_URL, 12)]
    assert extractor.calls  # the discovered post URL was extracted


def test_once_with_configured_anonymous_account_routes_to_anonymous_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Path, audit_log_path: Path
) -> None:
    """A default (`anonymous`) account routes through the anonymous backend."""
    account = AccountConfig(url=ACCOUNT_URL, backend="anonymous")
    config = _config_with(_source_config(accounts=[account]))
    _install_config(monkeypatch, config)

    anon = _ScriptedBackend(urls=[POST_URL])
    _install_backends(monkeypatch, anonymous=anon, hikerapi=_ExplodingBackend())
    _install_extractor(monkeypatch, _CountingExtractor())

    assert scheduler_cli.main(["--once", ACCOUNT_URL]) == 0
    assert anon.calls == [(ACCOUNT_URL, 12)]


def test_once_with_unconfigured_account_url_exits_2_with_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Path,
    audit_log_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An account URL not in `sources.yaml` exits `2` with a typed error line.

    Fork 5: config is the source of truth for backend routing; the CLI
    refuses to invent a default backend for an unknown account.
    """
    config = _config_with(_source_config())
    _install_config(monkeypatch, config)
    _install_backends(monkeypatch)
    _install_extractor(monkeypatch, _CountingExtractor())

    assert scheduler_cli.main(["--once", UNCONFIGURED_ACCOUNT_URL]) == 2
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["error_type"] == "unconfigured_account"
    assert payload["url"] == UNCONFIGURED_ACCOUNT_URL


def test_is_instagram_post_url_matches_p_and_reel_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot-check the shared discriminator used by the CLI is not accidentally broken.

    Belongs here as a CLI-boundary sanity: if `is_instagram_post_url` ever
    regresses, `--once` misroutes post URLs into the account-lookup branch.
    Belt-and-suspenders next to `tests/test_sources_config.py::PostConfig`.
    """
    from planazo.sources.config import is_instagram_post_url

    assert is_instagram_post_url(POST_URL) is True
    assert is_instagram_post_url(REEL_URL) is True
    assert is_instagram_post_url(ACCOUNT_URL) is False
    assert is_instagram_post_url("not-a-url") is False
