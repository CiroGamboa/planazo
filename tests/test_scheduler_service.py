"""Unit tests for `planazo.scheduler.service` — the tick service.

Covers:

- `run_tick` composition — empty config, post-only, account-only, mixed.
- `_scheduler_gate` observability — the four `gate_reason` literals fire
  from the four decision branches and land verbatim on the audit record
  (locks the M9 fix — same taxonomy across gate, record, and reader).
- Cadence + failure gating — `next_run_after` blocks a pending URL,
  `consecutive_failures >= 3` skips + resets, extraction failure bumps.
- M10 cadence-order swap — `PostConfig` cadence never gates a persisted
  post; `AccountConfig` cadence still gates discovery.
- M8 `--once`/`--tick` handoff — `_process_source_url` produces identical
  records under `bypass_cadence_gate=True` and `bypass_cadence_gate=False`
  after nulling the three per-invocation identity fields.
- M11 positional-callable convention — `run_tick` invokes the extractor
  as `extractor(url, delegator_user_id)` positional.
- Rule-2 leak channel — a `HikerClientError` carrying a caption fragment
  lands on `record.errors` as a regex-locked, truncated entry.
- Extractor-error record shape — failing extractions bump
  `consecutive_failures` and populate `errors` via `format_error_entry`.
- Backend routing — `AccountConfig.backend` picks between the two
  `InstagramDiscoveryProtocol` implementations.

Tests inject fakes at every seam: no real LLM call, no real DB file
(`":memory:"` throughout), no real HTTP.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planazo.catalog.models import Event
from planazo.catalog.repository import insert_event
from planazo.extraction.models import ExtractionResult
from planazo.extraction.multimodal_profile import ACCOUNT_SCAN, SINGLE_POST, MultimodalProfile
from planazo.scheduler.models import (
    ScanState,
    SchedulerRunRecord,
    TickReport,
)
from planazo.scheduler.repository import (
    SYSTEM_USER_TELEGRAM_ID,
    bootstrap_system_user,
    get_scan_state,
    upsert_scan_state,
)
from planazo.scheduler.service import (
    ExtractorCallable,
    _process_source_url,
    _scheduler_gate,
    run_tick,
)
from planazo.sources.config import (
    AccountConfig,
    MediaTypeFlags,
    PostConfig,
    SourceConfig,
    SourcesConfig,
)
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol
from planazo.sources.instagram.hiker_client import HikerClientError
from planazo.storage import db

POST_URL_A = "https://www.instagram.com/p/AAAAAA/"
POST_URL_B = "https://www.instagram.com/p/BBBBBB/"
ACCOUNT_URL = "https://www.instagram.com/curated.agenda/"

FIXED_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

ERRORS_ENTRY_REGEX = re.compile(
    r"^(unsupported_source|rate_limited|auth_failed|not_found|unsupported_media): [^\n\t]{0,120}$"
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A `sqlite3.Connection` open to a tmp-file DB with schema applied.

    File-backed rather than `":memory:"` because `run_tick` opens its own
    connection via `conn_factory` and closes it in `finally` — an
    in-memory DB would go away as soon as the tick returned, and the test
    could not inspect `scan_state` afterwards. A `tmp_path` file preserves
    the state and the test's own connection remains open for reads.
    """
    db_file = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "scheduler_runs.jsonl"


def _fixed_now() -> datetime:
    return FIXED_NOW


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


def _ok_result() -> ExtractionResult:
    now = FIXED_NOW
    event = Event(
        source="instagram",
        source_url=POST_URL_A,
        title="Test event",
        start_utc=now,
        end_utc=now,
        category="music",
        city="Barcelona",
        confidence=0.9,
    )
    return ExtractionResult(status="ok", events=[event], notes="")


def _error_result() -> ExtractionResult:
    return ExtractionResult(
        status="error",
        error_type="not_found",
        notes="not found on Meta",
    )


# ---- test doubles ----------------------------------------------------------


class CountingExtractor:
    """Records every call; returns a per-URL scripted response."""

    def __init__(self, script: dict[str, ExtractionResult] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.script: dict[str, ExtractionResult] = script or {}
        self.default: ExtractionResult = _ok_result()

    def __call__(self, url: str, delegator_user_id: int) -> ExtractionResult:
        self.calls.append((url, delegator_user_id))
        return self.script.get(url, self.default)


class ScriptedBackend:
    """Discovery-protocol stub: returns a canned list or raises a canned exception."""

    def __init__(
        self,
        *,
        urls: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.urls = urls or []
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        self.calls.append((account_url, limit))
        if self.raises is not None:
            raise self.raises
        return list(self.urls)


class ExplodingBackend:
    """Discovery-protocol stub that fails the test if called."""

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        raise AssertionError(
            f"unexpected discovery call: account_url={account_url!r} limit={limit}"
        )


def _backends(
    *,
    anonymous: InstagramDiscoveryProtocol | None = None,
    hikerapi: InstagramDiscoveryProtocol | None = None,
) -> dict:
    return {
        "anonymous": anonymous or ExplodingBackend(),
        "hikerapi": hikerapi or ExplodingBackend(),
    }


def _run_tick_defaults(
    conn: sqlite3.Connection,
    config: SourcesConfig,
    audit_log: Path,
    *,
    extractor: ExtractorCallable | None = None,
    backends: dict | None = None,
) -> TickReport:
    # `run_tick` opens + closes its own connection via `conn_factory`. The
    # `conn` fixture parameter's side effect is monkeypatching `db.DB_PATH`
    # to the shared tmp file so this fresh connection sees the same rows
    # the fixture's connection sees.
    del conn  # fixture keeps DB_PATH monkeypatched for the fresh conn below

    def _factory() -> sqlite3.Connection:
        return db.connect()

    return run_tick(
        now=_fixed_now,
        conn_factory=_factory,
        source_by_name={},
        backends=backends or _backends(),
        extractor=extractor or CountingExtractor(),
        config=config,
        audit_log_path=audit_log,
    )


# ---- run_tick composition --------------------------------------------------


def test_tick_with_empty_config_produces_empty_report(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    report = _run_tick_defaults(conn, _config_with(_source_config()), audit_log)
    assert report.records == []
    assert report.total_events_extracted == 0


def test_tick_with_no_instagram_block_produces_empty_report(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    report = _run_tick_defaults(conn, SourcesConfig(), audit_log)
    assert report.records == []


def test_tick_calls_extractor_for_each_configured_post_on_first_run(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    extractor = CountingExtractor()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    report = _run_tick_defaults(conn, config, audit_log, extractor=extractor)

    assert len(extractor.calls) == 1
    (url, uid) = extractor.calls[0]
    assert url == POST_URL_A
    assert uid > 0
    assert report.total_events_extracted == 1
    assert len(report.records) == 1


def test_tick_writes_one_record_per_configured_post(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    config = _config_with(
        _source_config(posts=[PostConfig(url=POST_URL_A), PostConfig(url=POST_URL_B)])
    )
    report = _run_tick_defaults(conn, config, audit_log)
    assert len(report.records) == 2
    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_tick_writes_one_record_per_configured_account(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    backend = ScriptedBackend(urls=[POST_URL_A])
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    assert len(report.records) == 1
    assert report.records[0].source_kind == "account"


def test_tick_record_source_kind_matches_config_block(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    backend = ScriptedBackend(urls=[POST_URL_B])
    config = _config_with(
        _source_config(
            posts=[PostConfig(url=POST_URL_A)],
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    kinds = [rec.source_kind for rec in report.records]
    assert kinds == ["post", "account"]


def test_tick_record_backend_field_is_none_for_post_entries(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    report = _run_tick_defaults(conn, config, audit_log)
    assert report.records[0].backend is None


def test_tick_record_backend_field_populated_for_account_entries(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    backend = ScriptedBackend(urls=[POST_URL_A])
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="hikerapi")])
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(hikerapi=backend))
    assert report.records[0].backend == "hikerapi"


def test_tick_bootstraps_system_user_on_first_call(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    _run_tick_defaults(conn, _config_with(_source_config()), audit_log)
    row = conn.execute(
        "SELECT telegram_user_id FROM users WHERE telegram_user_id = ?",
        (SYSTEM_USER_TELEGRAM_ID,),
    ).fetchone()
    assert row is not None


# ---- idempotency + repeat-tick behaviour -----------------------------------


def test_tick_skips_extraction_when_events_exist_for_source_url_returns_non_empty(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    insert_event(
        conn,
        Event(
            source="instagram",
            source_url=POST_URL_A,
            title="already persisted",
            start_utc=FIXED_NOW,
            end_utc=FIXED_NOW,
            category="music",
            city="Barcelona",
            confidence=0.9,
        ),
    )
    extractor = CountingExtractor()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    report = _run_tick_defaults(conn, config, audit_log, extractor=extractor)

    assert extractor.calls == []
    assert report.records[0].posts_skipped_idempotent == 1


def test_tick_idempotent_when_run_twice(conn: sqlite3.Connection, audit_log: Path) -> None:
    extractor = CountingExtractor()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)

    # First tick called the extractor; second tick short-circuited on the
    # persisted event and did NOT — the primary acceptance behavior.
    assert len(extractor.calls) == 1


def test_second_tick_produces_zero_extractor_calls(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    extractor = CountingExtractor()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)
    calls_after_first = list(extractor.calls)

    _run_tick_defaults(conn, config, audit_log, extractor=extractor)
    assert extractor.calls == calls_after_first  # nothing added on second tick


# ---- cadence gating (account URLs — cadence-first ordering) ---------------


def test_tick_respects_next_run_after_cadence_gate(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    # AccountConfig cadence gates discovery — we pre-seed the state so
    # cadence has NOT elapsed yet, and assert the backend is never called.
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            last_scanned_at=FIXED_NOW - timedelta(hours=1),
            last_success_at=FIXED_NOW - timedelta(hours=1),
            consecutive_failures=0,
        ),
    )
    backend = ExplodingBackend()  # asserts if called
    config = _config_with(
        _source_config(
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
            default_cadence=timedelta(hours=6),
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))

    assert report.records[0].gate_reason == "cadence_not_ready"
    assert report.records[0].posts_discovered == 0


def test_tick_runs_when_cadence_elapsed(conn: sqlite3.Connection, audit_log: Path) -> None:
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            last_scanned_at=FIXED_NOW - timedelta(hours=7),
            last_success_at=FIXED_NOW - timedelta(hours=7),
            consecutive_failures=0,
        ),
    )
    backend = ScriptedBackend(urls=[POST_URL_A])
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
            default_cadence=timedelta(hours=6),
        )
    )
    report = _run_tick_defaults(
        conn,
        config,
        audit_log,
        extractor=extractor,
        backends=_backends(anonymous=backend),
    )

    assert report.records[0].gate_reason == "cadence_ready"
    assert extractor.calls == [(POST_URL_A, extractor.calls[0][1])]


def test_tick_skips_when_consecutive_failures_ge_3(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    upsert_scan_state(
        conn,
        ScanState(source_url=ACCOUNT_URL, consecutive_failures=3),
    )
    backend = ExplodingBackend()
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    report = _run_tick_defaults(
        conn,
        config,
        audit_log,
        extractor=extractor,
        backends=_backends(anonymous=backend),
    )

    assert report.records[0].gate_reason == "failure_skip"
    assert extractor.calls == []

    # Per ADR 0011 §D9 — the counter resets on the skip tick.
    state = get_scan_state(conn, ACCOUNT_URL)
    assert state is not None
    assert state.consecutive_failures == 0


def test_failure_skip_fires_admin_notifier_with_pre_reset_counter(
    conn: sqlite3.Connection, audit_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0022: `failure_skip` gate fires the admin threshold-trigger DM.

    The notifier receives the PRE-reset counter (the value that
    triggered the skip), not the post-reset zero, so the operator
    sees how deep the URL was into failure before the skip took over.
    """
    upsert_scan_state(
        conn,
        ScanState(source_url=ACCOUNT_URL, consecutive_failures=5),
    )
    captured: list[tuple[str, int]] = []

    def fake_notify(source_url: str, consecutive_failures: int) -> None:
        captured.append((source_url, consecutive_failures))

    # Patch the imported name inside service.py (where it is actually
    # called), not the origin module — mirrors curator notifier tests.
    from planazo.scheduler import service as scheduler_service

    monkeypatch.setattr(scheduler_service, "notify_admins_of_failure_skip", fake_notify)

    backend = ExplodingBackend()
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    _run_tick_defaults(
        conn,
        config,
        audit_log,
        extractor=extractor,
        backends=_backends(anonymous=backend),
    )

    assert captured == [(ACCOUNT_URL, 5)]


def test_failure_skip_notifier_exception_does_not_break_the_tick(
    conn: sqlite3.Connection, audit_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising notifier never propagates — Rule 4 belt-and-braces.

    The `SchedulerRunRecord` still lands and `scan_state` still resets.
    """
    upsert_scan_state(
        conn,
        ScanState(source_url=ACCOUNT_URL, consecutive_failures=3),
    )

    def raising_notify(source_url: str, consecutive_failures: int) -> None:
        raise RuntimeError("simulated Telegram outage")

    from planazo.scheduler import service as scheduler_service

    monkeypatch.setattr(scheduler_service, "notify_admins_of_failure_skip", raising_notify)

    backend = ExplodingBackend()
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    report = _run_tick_defaults(
        conn,
        config,
        audit_log,
        extractor=extractor,
        backends=_backends(anonymous=backend),
    )

    # Tick still completed; audit record still landed; counter still reset.
    assert report.records[0].gate_reason == "failure_skip"
    state = get_scan_state(conn, ACCOUNT_URL)
    assert state is not None
    assert state.consecutive_failures == 0


def test_cadence_not_ready_does_not_fire_notifier(
    conn: sqlite3.Connection, audit_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `failure_skip` fires the trigger; `cadence_not_ready` is silent.

    The threshold trigger is scoped to threshold-crossings, not to any
    skip. A URL still within its cadence window never notifies the admin.
    """
    # Mark the URL as recently scanned so cadence_not_ready fires.
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            consecutive_failures=0,
            last_scanned_at=FIXED_NOW,
        ),
    )
    captured: list[tuple[str, int]] = []

    def fake_notify(source_url: str, consecutive_failures: int) -> None:
        captured.append((source_url, consecutive_failures))

    from planazo.scheduler import service as scheduler_service

    monkeypatch.setattr(scheduler_service, "notify_admins_of_failure_skip", fake_notify)

    backend = ExplodingBackend()
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    report = _run_tick_defaults(
        conn,
        config,
        audit_log,
        extractor=extractor,
        backends=_backends(anonymous=backend),
    )

    assert report.records[0].gate_reason == "cadence_not_ready"
    assert captured == []


def test_tick_increments_consecutive_failures_on_extraction_error(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    extractor = CountingExtractor(script={POST_URL_A: _error_result()})
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)
    state = get_scan_state(conn, POST_URL_A)
    assert state is not None
    assert state.consecutive_failures == 1


def test_tick_resets_consecutive_failures_on_extraction_success(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    upsert_scan_state(conn, ScanState(source_url=POST_URL_A, consecutive_failures=2))
    extractor = CountingExtractor()  # default is ok
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)
    state = get_scan_state(conn, POST_URL_A)
    assert state is not None
    assert state.consecutive_failures == 0
    assert state.last_success_at == FIXED_NOW


# ---- backend routing --------------------------------------------------------


def test_tick_routes_hikerapi_account_via_hikerapi_backend(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    hikerapi = ScriptedBackend(urls=[POST_URL_A])
    anonymous = ExplodingBackend()  # must NOT be called
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="hikerapi")])
    )
    _run_tick_defaults(
        conn, config, audit_log, backends=_backends(hikerapi=hikerapi, anonymous=anonymous)
    )
    assert hikerapi.calls == [(ACCOUNT_URL, 12)]


def test_tick_routes_anonymous_account_via_anonymous_backend(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    hikerapi = ExplodingBackend()
    anonymous = ScriptedBackend(urls=[POST_URL_A])
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    _run_tick_defaults(
        conn, config, audit_log, backends=_backends(hikerapi=hikerapi, anonymous=anonymous)
    )
    assert anonymous.calls == [(ACCOUNT_URL, 12)]


# ---- gate observability (M9) ----------------------------------------------


def test_scheduler_gate_first_run() -> None:
    should_run, reason = _scheduler_gate(None, timedelta(hours=6), now=_fixed_now)
    assert should_run is True
    assert reason == "first_run"


def test_scheduler_gate_cadence_ready() -> None:
    state = ScanState(
        source_url=POST_URL_A,
        last_scanned_at=FIXED_NOW - timedelta(hours=7),
        last_success_at=FIXED_NOW - timedelta(hours=7),
        consecutive_failures=0,
    )
    should_run, reason = _scheduler_gate(state, timedelta(hours=6), now=_fixed_now)
    assert should_run is True
    assert reason == "cadence_ready"


def test_scheduler_gate_cadence_not_ready() -> None:
    state = ScanState(
        source_url=POST_URL_A,
        last_scanned_at=FIXED_NOW - timedelta(hours=1),
        last_success_at=FIXED_NOW - timedelta(hours=1),
        consecutive_failures=0,
    )
    should_run, reason = _scheduler_gate(state, timedelta(hours=6), now=_fixed_now)
    assert should_run is False
    assert reason == "cadence_not_ready"


def test_scheduler_gate_failure_skip() -> None:
    state = ScanState(source_url=POST_URL_A, consecutive_failures=3)
    should_run, reason = _scheduler_gate(state, timedelta(hours=6), now=_fixed_now)
    assert should_run is False
    assert reason == "failure_skip"


def test_tick_record_gate_reason_first_run(conn: sqlite3.Connection, audit_log: Path) -> None:
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    report = _run_tick_defaults(conn, config, audit_log)
    assert report.records[0].gate_reason == "first_run"


def test_tick_record_gate_reason_cadence_ready(conn: sqlite3.Connection, audit_log: Path) -> None:
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            last_scanned_at=FIXED_NOW - timedelta(hours=8),
            last_success_at=FIXED_NOW - timedelta(hours=8),
            consecutive_failures=0,
        ),
    )
    backend = ScriptedBackend(urls=[POST_URL_A])
    config = _config_with(
        _source_config(
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
            default_cadence=timedelta(hours=6),
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    assert report.records[0].gate_reason == "cadence_ready"


def test_tick_record_gate_reason_cadence_not_ready(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            last_scanned_at=FIXED_NOW - timedelta(hours=1),
            last_success_at=FIXED_NOW - timedelta(hours=1),
            consecutive_failures=0,
        ),
    )
    backend = ExplodingBackend()
    config = _config_with(
        _source_config(
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
            default_cadence=timedelta(hours=6),
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    record = report.records[0]
    assert record.gate_reason == "cadence_not_ready"
    assert record.posts_discovered == 0
    assert record.posts_extracted_ok == 0
    assert record.posts_extracted_error == 0
    assert record.posts_skipped_idempotent == 0


def test_tick_record_gate_reason_failure_skip(conn: sqlite3.Connection, audit_log: Path) -> None:
    upsert_scan_state(
        conn,
        ScanState(source_url=ACCOUNT_URL, consecutive_failures=3),
    )
    backend = ExplodingBackend()
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")])
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    assert report.records[0].gate_reason == "failure_skip"


# ---- PostConfig cadence-order swap (M10) ----------------------------------


def test_post_config_cadence_does_not_gate_successful_post_on_repeat_tick(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """Locks M10 — for `PostConfig`, idempotency fires BEFORE cadence.

    Pre-populate `scan_state.last_scanned_at = now - 1h` with `cadence = 6h`
    (would normally block cadence-first) AND pre-populate the events table
    with a row for the URL (the idempotency layer). Assert (a) the
    extractor is never called; (b) the record has `posts_skipped_idempotent=1`
    and `gate_reason="cadence_ready"` (the would-have-run branch — cadence
    never blocked a persisted post).
    """
    insert_event(
        conn,
        Event(
            source="instagram",
            source_url=POST_URL_A,
            title="already persisted",
            start_utc=FIXED_NOW,
            end_utc=FIXED_NOW,
            category="music",
            city="Barcelona",
            confidence=0.9,
        ),
    )
    upsert_scan_state(
        conn,
        ScanState(
            source_url=POST_URL_A,
            last_scanned_at=FIXED_NOW - timedelta(hours=1),
            last_success_at=FIXED_NOW - timedelta(hours=1),
            consecutive_failures=0,
        ),
    )
    extractor = CountingExtractor()
    config = _config_with(
        _source_config(
            posts=[PostConfig(url=POST_URL_A, cadence=timedelta(hours=6))],
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, extractor=extractor)

    record = report.records[0]
    assert extractor.calls == []
    assert record.posts_skipped_idempotent == 1
    assert record.gate_reason == "cadence_ready"


def test_account_config_cadence_gates_discovery_when_pending(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """Locks the M10 contrast — accounts still cadence-first.

    Discovery is a per-account rate-limit surface; cadence must protect it
    even when the account has never produced any events. This test is the
    negative to `test_post_config_cadence_does_not_gate_successful_post_on_repeat_tick`.
    """
    upsert_scan_state(
        conn,
        ScanState(
            source_url=ACCOUNT_URL,
            last_scanned_at=FIXED_NOW - timedelta(hours=1),
            last_success_at=FIXED_NOW - timedelta(hours=1),
            consecutive_failures=0,
        ),
    )
    backend = ExplodingBackend()  # cadence blocks; discovery must NOT run
    config = _config_with(
        _source_config(
            accounts=[AccountConfig(url=ACCOUNT_URL, backend="anonymous")],
            default_cadence=timedelta(hours=6),
        )
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(anonymous=backend))
    assert report.records[0].gate_reason == "cadence_not_ready"
    assert report.records[0].posts_discovered == 0


# ---- --once/--tick handoff (M8) -------------------------------------------


def test_once_and_tick_produce_identical_record_shapes_for_same_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locks M8 — `bypass_cadence_gate=True/False` differ only where cadence lives.

    Fresh in-memory DB + fresh URL: the --once record (bypass=True) and
    the --tick record (bypass=False → first_run) must be byte-identical
    after nulling `run_id`, `started_at`, `ended_at` (the three fields
    that legitimately differ per invocation).
    """
    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    def _process(bypass: bool) -> SchedulerRunRecord:
        connection = db.connect()
        try:
            system_user = bootstrap_system_user(connection)
            assert system_user.id is not None
            audit = tmp_path / f"runs-{bypass}.jsonl"
            return _process_source_url(
                conn=connection,
                source_url=POST_URL_A,
                source_kind="post",
                cadence=timedelta(hours=6),
                backend_client=None,
                backend_name=None,
                extractor=CountingExtractor(),
                now=_fixed_now,
                audit_log_path=audit,
                system_user_id=system_user.id,
                bypass_cadence_gate=bypass,
            )
        finally:
            connection.close()

    once_record = _process(True)
    tick_record = _process(False)

    def _null_identity(rec: SchedulerRunRecord) -> dict[str, object]:
        payload = rec.model_dump()
        payload["run_id"] = ""
        payload["started_at"] = None
        payload["ended_at"] = None
        return payload

    assert _null_identity(once_record) == _null_identity(tick_record)


# ---- extractor callable convention (M11) ----------------------------------


def test_run_tick_accepts_positional_extractor_callable(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """Locks M11 — the extractor is invoked positionally as (url, uid).

    A lambda that unpacks positional args is the canonical wire for
    `agents.extractor.extract_once` (keyword-only for `delegator_user_id`).
    If `run_tick` starts passing `delegator_user_id=...` keyword-only the
    lambda breaks — the test fails.
    """
    seen: list[tuple[str, int]] = []

    def positional_extractor(url: str, uid: int) -> ExtractionResult:
        seen.append((url, uid))
        return _ok_result()

    extractor: ExtractorCallable = positional_extractor
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))
    _run_tick_defaults(conn, config, audit_log, extractor=extractor)

    assert len(seen) == 1
    assert seen[0][0] == POST_URL_A
    assert seen[0][1] > 0


# ---- Rule-2 leak channel (B3) at the service layer ------------------------


def test_discovery_backend_error_writes_regex_locked_entry(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """A HikerAPI failure carrying caption bytes lands as a regex-locked entry.

    `format_error_entry` sanitises the exception message; the model-level
    regex validator holds the boundary a second time. Assert (a) the
    record has exactly one `errors` entry, (b) it matches the strict
    canonical regex, (c) the caption bytes past 120 chars did not slip
    through.
    """
    poisoned = (
        "429 for user @business_venue with caption: come see us at C/ "
        "Balmes 12 for Friday's set — DJ Serdlic playing until 4am, "
        "drinks 2x1 all night long"
    )
    backend = ScriptedBackend(raises=HikerClientError("rate_limited", poisoned))
    config = _config_with(
        _source_config(accounts=[AccountConfig(url=ACCOUNT_URL, backend="hikerapi")])
    )
    report = _run_tick_defaults(conn, config, audit_log, backends=_backends(hikerapi=backend))
    record = report.records[0]

    assert len(record.errors) == 1
    entry = record.errors[0]
    assert ERRORS_ENTRY_REGEX.fullmatch(entry) is not None
    assert "\n" not in entry
    assert "\t" not in entry
    # Everything past the 120-char detail window is dropped — canonical shape.
    assert entry.startswith("rate_limited: ")
    assert len(entry) - len("rate_limited: ") <= 120


# ---- audit-log writes ------------------------------------------------------


def test_tick_writes_audit_log_line_per_record(conn: sqlite3.Connection, audit_log: Path) -> None:
    config = _config_with(
        _source_config(posts=[PostConfig(url=POST_URL_A), PostConfig(url=POST_URL_B)])
    )
    report = _run_tick_defaults(conn, config, audit_log)
    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(report.records) == 2

    # Every line round-trips back through the model.
    for line in lines:
        rec = SchedulerRunRecord.model_validate_json(line)
        assert rec.source_kind == "post"


# ---- extractor_factory profile dispatch -----------------------------------


class _ProfileRecordingFactory:
    """Records the `MultimodalProfile` each `_process_*_entry` requests.

    Returns a `CountingExtractor` each call so the audit-log path stays
    lit; the recorded `profiles` list is what tests assert on.
    """

    def __init__(self) -> None:
        self.profiles: list[MultimodalProfile] = []
        self.extractor = CountingExtractor()

    def __call__(self, profile: MultimodalProfile) -> ExtractorCallable:
        self.profiles.append(profile)
        return self.extractor


def test_run_tick_passes_single_post_profile_for_configured_post_entries(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """A `posts:` entry has no account context — `_process_post_entry` asks
    the factory for `SINGLE_POST`. The account-scan cap is not applied to
    single-post work."""
    del conn
    factory = _ProfileRecordingFactory()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))

    def _factory() -> sqlite3.Connection:
        return db.connect()

    run_tick(
        now=_fixed_now,
        conn_factory=_factory,
        source_by_name={},
        backends=_backends(),
        extractor=CountingExtractor(),  # fallback if factory is `None` (not this test)
        extractor_factory=factory,
        config=config,
        audit_log_path=audit_log,
    )

    assert factory.profiles == [SINGLE_POST]


def test_run_tick_passes_account_scan_profile_for_account_entries(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """An `accounts:` entry without per-account overrides gets the
    `ACCOUNT_SCAN` preset — the higher-cap profile roundup posts need."""
    del conn
    factory = _ProfileRecordingFactory()
    account = AccountConfig(url=ACCOUNT_URL, backend="anonymous")
    config = _config_with(_source_config(accounts=[account]))

    def _factory() -> sqlite3.Connection:
        return db.connect()

    run_tick(
        now=_fixed_now,
        conn_factory=_factory,
        source_by_name={},
        backends=_backends(anonymous=ScriptedBackend(urls=[])),
        extractor=CountingExtractor(),
        extractor_factory=factory,
        config=config,
        audit_log_path=audit_log,
    )

    assert factory.profiles == [ACCOUNT_SCAN]


def test_run_tick_folds_per_account_override_into_resolved_profile(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """A `sources.yaml` account with `max_carousel_images: 15` — the roundup
    shape — makes the factory receive a profile with that cap on top of
    `ACCOUNT_SCAN`'s reel default. This is the load-bearing wire from YAML
    to `_multimodal_hook`."""
    del conn
    factory = _ProfileRecordingFactory()
    account = AccountConfig(
        url=ACCOUNT_URL,
        backend="anonymous",
        max_carousel_images=15,
    )
    config = _config_with(_source_config(accounts=[account]))

    def _factory() -> sqlite3.Connection:
        return db.connect()

    run_tick(
        now=_fixed_now,
        conn_factory=_factory,
        source_by_name={},
        backends=_backends(anonymous=ScriptedBackend(urls=[])),
        extractor=CountingExtractor(),
        extractor_factory=factory,
        config=config,
        audit_log_path=audit_log,
    )

    assert len(factory.profiles) == 1
    resolved = factory.profiles[0]
    assert resolved.max_carousel_images == 15
    assert resolved.max_reel_frames == ACCOUNT_SCAN.max_reel_frames


def test_run_tick_without_factory_falls_back_to_extractor(
    conn: sqlite3.Connection, audit_log: Path
) -> None:
    """Backwards-compat: `extractor_factory=None` (the default) means every
    test that pre-dates this ticket keeps using its fixed `extractor` fake
    verbatim — no profile is threaded through and no unexpected import
    happens."""
    del conn
    extractor = CountingExtractor()
    config = _config_with(_source_config(posts=[PostConfig(url=POST_URL_A)]))

    def _factory() -> sqlite3.Connection:
        return db.connect()

    run_tick(
        now=_fixed_now,
        conn_factory=_factory,
        source_by_name={},
        backends=_backends(),
        extractor=extractor,
        config=config,
        audit_log_path=audit_log,
    )

    assert extractor.calls  # extractor was invoked
    # No factory means no profile dispatch — the fixed extractor was used
    # verbatim without asking anyone for a profile.
