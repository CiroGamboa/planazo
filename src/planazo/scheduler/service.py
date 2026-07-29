"""The tick service — the scheduler's per-tick composition root.

`run_tick` is the pure-composition entry point: it opens one connection per
tick, bootstraps the system user, and dispatches each configured post URL and
account URL to `_process_source_url`. `_process_source_url` is the per-URL
work unit shared between `run_tick` (with `bypass_cadence_gate=False`) and the
`--once` CLI (Stage 4, with `bypass_cadence_gate=True`) so there is exactly
one place that decides the ordering of cadence gating, discovery, idempotency
pre-check, extractor dispatch, and audit-log writing.

Every persisted `SchedulerRunRecord.errors` entry is built through
[`format_error_entry`][planazo.scheduler.models.format_error_entry]. AGENTS.md
rule 2 leak channel: a wrapped `HikerClientError` / `AnonInstagramClientError`
may carry a caption fragment inside `str(exc)`; the helper truncates it to
`TRUNCATE_LEN` and strips newlines / tabs, and the model-level regex
validator holds the boundary a second time.

Two decisions the plan calls out that live here as behaviour, not in the
data model:

- **Cadence-order swap for `PostConfig`.** A configured post URL runs the
  idempotency pre-check BEFORE the cadence gate. `PostConfig.cadence` only
  gates failure-retry — once the URL is persisted the composite
  `UNIQUE(source_url, event_index_in_post)` in `events` locks it out and
  cadence never needs to. `AccountConfig` keeps cadence-first ordering
  because discovery is a per-account rate-limit surface — cadence must
  protect it.
- **`bypass_cadence_gate` on `_process_source_url`.** The `--once` CLI
  invokes the same helper with the flag flipped so an operator's diagnostic
  call always runs against the current state of the DB. Idempotency, audit
  log writes, and `scan_state.consecutive_failures` bookkeeping still apply
  — `--once` is diagnostic but its side effects are real.

[ADR 0011](../../../../../docs/adr/0011-scheduled-ingestion.md) §D8 (partially
superseded by ADR 0014 §D8 marker) locks the audit-log grain at one record
per source URL processed;
[ADR 0014](../../../../../docs/adr/0014-instagram-discovery-backends.md)
locks the two-backend routing via `AccountConfig.backend`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from planazo.catalog.repository import events_exist_for_source_url
from planazo.extraction.models import ExtractionResult
from planazo.interfaces.sources import EventSource
from planazo.scheduler.audit import append_run_record
from planazo.scheduler.models import (
    GateReason,
    ScanState,
    SchedulerBackend,
    SchedulerRunRecord,
    SourceKind,
    TickReport,
    format_error_entry,
)
from planazo.scheduler.notifier import notify_admins_of_failure_skip
from planazo.scheduler.repository import (
    bootstrap_system_user,
    get_scan_state,
    upsert_scan_state,
)
from planazo.sources.base import ErrorType, next_run_after
from planazo.sources.config import AccountConfig, PostConfig, SourceConfig, SourcesConfig
from planazo.sources.instagram.anon_client import AnonInstagramClientError
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol
from planazo.sources.instagram.hiker_client import HikerClientError

__all__ = [
    "ExtractorCallable",
    "run_tick",
]

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_SKIP_THRESHOLD: Final[int] = 3
"""How many consecutive failed ticks before a source URL skips one round.

Per ADR 0011 §D9 — three failed ticks in a row skips the URL for one tick
and resets the counter on the tick after that (so a permanently broken URL
gets exactly one attempt per two tick-intervals).
"""

DISCOVERY_LIMIT: Final[int] = 12
"""How many recent post URLs to fetch from an account's discovery backend."""


ExtractorCallable = Callable[[str, int], ExtractionResult]
"""The positional callable shape `run_tick` invokes for each URL.

Positional arguments: `(url: str, delegator_user_id: int)`. The composition
root normalises `agents.extractor.extract_once` (which is keyword-only for
`delegator_user_id`) to this shape via a one-line lambda at wire time:

    extractor = lambda url, uid: extract_once(url, delegator_user_id=uid)

Tests inject fakes that satisfy the same positional shape without touching
the real LLM path. The type alias exists so a downstream refactor renaming
the callable cannot silently drift — `test_run_tick_accepts_positional_extractor_callable`
locks the shape at the type layer, `test_scheduler_types.py` locks it at
the mypy-strict layer.
"""


def _scheduler_gate(
    state: ScanState | None,
    cadence: timedelta,
    *,
    now: Callable[[], datetime],
) -> tuple[bool, GateReason]:
    """Decide whether the scheduler should run extraction for a source URL.

    Pure function — no I/O. Returns a `(should_run, gate_reason)` pair whose
    `gate_reason` component is the exact `Literal` `SchedulerRunRecord.gate_reason`
    stores, so the observability taxonomy stays identical across the gate,
    the record, and the audit-log reader.

    Four branches:

    - `state is None` → `(True, "first_run")`. Never seen this URL before.
    - `state.consecutive_failures >= CONSECUTIVE_FAILURE_SKIP_THRESHOLD` →
      `(False, "failure_skip")`. Per ADR 0011 §D9.
    - `now() >= next_run_after(cadence, state.last_scanned_at, now=now)` →
      `(True, "cadence_ready")`. The cadence window has elapsed.
    - Otherwise → `(False, "cadence_not_ready")`. Still within the cadence
      window; the caller writes a no-op record and returns.
    """
    if state is None:
        return (True, "first_run")
    if state.consecutive_failures >= CONSECUTIVE_FAILURE_SKIP_THRESHOLD:
        return (False, "failure_skip")
    when_ready = next_run_after(cadence, state.last_scanned_at, now=now)
    if now() >= when_ready:
        return (True, "cadence_ready")
    return (False, "cadence_not_ready")


def _empty_record(
    *,
    source_url: str,
    source_kind: SourceKind,
    backend: SchedulerBackend | None,
    gate_reason: GateReason,
    started_at: datetime,
    ended_at: datetime,
) -> SchedulerRunRecord:
    """Build a zero-counter `SchedulerRunRecord` — the shape for skipped URLs.

    Used for the two "did not run" gate branches (`cadence_not_ready`,
    `failure_skip`). Every counter is zero and `errors` is empty because
    nothing was attempted for this URL on this tick.
    """
    return SchedulerRunRecord(
        run_id=str(uuid4()),
        source_url=source_url,
        source_kind=source_kind,
        backend=backend,
        gate_reason=gate_reason,
        posts_discovered=0,
        posts_extracted_ok=0,
        posts_extracted_error=0,
        posts_skipped_idempotent=0,
        # `errors` defaults to `[]` via SchedulerRunRecord.model_config;
        # omitting it here keeps the grep gate green — every entry that
        # DOES land in this list goes through `format_error_entry`.
        started_at=started_at,
        ended_at=ended_at,
    )


def _resolve_gate_reason_for_bypass(state: ScanState | None) -> GateReason:
    """The `gate_reason` a bypass invocation (`--once`) writes on the record.

    `state is None` → `first_run`; otherwise `cadence_ready`. Bypass never
    writes a "did not run" reason because bypass, by construction, runs.
    """
    return "first_run" if state is None else "cadence_ready"


def _upsert_after_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    now_value: datetime,
    previous_state: ScanState | None,
    had_extraction_success: bool,
    had_error: bool,
) -> None:
    """Persist the new `scan_state` row after a URL has actually been processed.

    `last_scanned_at` is always the tick's `now`. `last_success_at` is
    stamped when an extraction returned `status="ok"`; otherwise it stays
    at the previous value so a healthy URL keeps its historical success
    timestamp even if the current tick was a no-op-skip.
    `consecutive_failures` bumps by one when the tick produced any error
    (discovery-side or extraction-side); a clean tick (success, idempotent
    skip, or discovery-returned-zero without error) resets it to zero.
    """
    previous_success = previous_state.last_success_at if previous_state is not None else None
    previous_failures = previous_state.consecutive_failures if previous_state is not None else 0

    last_success_at = now_value if had_extraction_success else previous_success
    consecutive_failures = previous_failures + 1 if had_error else 0

    upsert_scan_state(
        conn,
        ScanState(
            source_url=source_url,
            last_scanned_at=now_value,
            last_success_at=last_success_at,
            consecutive_failures=consecutive_failures,
        ),
    )


def _reset_failure_counter_on_gate_skip(conn: sqlite3.Connection, *, source_url: str) -> None:
    """Reset `scan_state.consecutive_failures` on a `failure_skip` tick.

    ADR 0011 §D9: after three consecutive failures the URL skips one tick
    and the counter resets on that skip tick so the next tick gets a fresh
    attempt. Uses a targeted UPDATE — the row already exists (the caller
    only reaches this branch when a `scan_state` row is present).
    """
    conn.execute(
        "UPDATE scan_state SET consecutive_failures = 0 WHERE source_url = ?",
        (source_url,),
    )
    conn.commit()


_SHARED_EXTRACTION_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {
        "unsupported_source",
        "rate_limited",
        "auth_failed",
        "not_found",
        "unsupported_media",
    }
)


def _extraction_error_type_or_default(result: ExtractionResult) -> ErrorType:
    """Return the `ErrorType` prefix a failed `ExtractionResult` maps to.

    The extractor's `error_type` taxonomy is a superset of the scheduler's
    (it adds `low_confidence_extraction`, `missing_date`, etc.). Only the
    five shared prefixes match `SchedulerRunRecord.errors`' regex; anything
    else collapses to `not_found` so the entry can still be persisted
    without leaking a caption-bearing exception message.
    """
    et = result.error_type
    if et is not None and et in _SHARED_EXTRACTION_ERROR_TYPES:
        # `et` is guaranteed to be one of the five shared literals by the
        # membership test; the cast tells mypy what the runtime already
        # knows without widening `ErrorType` on the return signature.
        return cast(ErrorType, et)
    return "not_found"


def _process_url_extraction(
    *,
    conn: sqlite3.Connection,
    urls_to_extract: list[str],
    extractor: ExtractorCallable,
    system_user_id: int,
) -> tuple[int, int, int, list[str]]:
    """Run extraction against every URL that survived the idempotency pre-check.

    Returns a 4-tuple `(ok_count, error_count, skipped_idempotent_count, error_entries)`.
    Each URL is checked one more time against `events_exist_for_source_url`
    inside this loop (the pre-check happens once at the caller for the
    post-config case, but for discovered account URLs the pre-check is
    per-URL and lives here).
    """
    ok_count = 0
    error_count = 0
    skipped = 0
    error_entries: list[str] = []
    for url in urls_to_extract:
        if events_exist_for_source_url(conn, url):
            skipped += 1
            continue
        result = extractor(url, system_user_id)
        if result.status == "ok":
            ok_count += 1
        else:
            error_count += 1
            prefix = _extraction_error_type_or_default(result)
            detail = result.notes or "no notes"
            error_entries.append(format_error_entry(prefix, detail))
    return ok_count, error_count, skipped, error_entries


def _process_source_url(
    *,
    conn: sqlite3.Connection,
    source_url: str,
    source_kind: SourceKind,
    cadence: timedelta,
    backend_client: InstagramDiscoveryProtocol | None,
    backend_name: SchedulerBackend | None,
    extractor: ExtractorCallable,
    now: Callable[[], datetime],
    audit_log_path: Path,
    system_user_id: int,
    bypass_cadence_gate: bool,
    discovery_limit: int | None = None,
) -> SchedulerRunRecord:
    """Process one configured source URL end-to-end; return the audit record.

    The shared work unit `run_tick` and the Stage-4 `--once` CLI both invoke.
    Ordering:

    1. `source_kind == "post"` — idempotency pre-check runs FIRST. If the
       URL already has events, the record is a `posts_skipped_idempotent=1`
       no-op with `gate_reason="cadence_ready"` (the would-have-run branch)
       and no cadence bookkeeping. This locks the M10 plan fix — cadence
       must never block a successful post.
    2. Cadence gate (unless `bypass_cadence_gate=True`). `cadence_not_ready`
       and `failure_skip` write a zero-counter record and return.
    3. Discovery (account-only). The configured backend runs
       `list_recent_posts`. Backend errors write a record and bump the
       failure counter.
    4. Extraction. For every discovered URL (or the single post URL) the
       extractor is invoked positionally as `extractor(url, system_user_id)`.
       Per-URL idempotency check inside the loop for accounts.
    5. `scan_state` update + audit-log write.
    """
    started_at = now()

    state = get_scan_state(conn, source_url)

    # M10 — post-config idempotency BEFORE cadence. A successful post is
    # locked out by `events_exist_for_source_url` (the UNIQUE constraint on
    # `events` closes the second layer if the pre-check races on a fresh
    # dev DB). Cadence never blocks a persisted post; it only gates the
    # failure-retry path.
    if source_kind == "post" and events_exist_for_source_url(conn, source_url):
        ended_at = now()
        record = SchedulerRunRecord(
            run_id=str(uuid4()),
            source_url=source_url,
            source_kind=source_kind,
            backend=None,
            gate_reason=_resolve_gate_reason_for_bypass(state),
            posts_discovered=0,
            posts_extracted_ok=0,
            posts_extracted_error=0,
            posts_skipped_idempotent=1,
            # `errors` defaults to `[]` via SchedulerRunRecord.model_config;
            # omitting it here keeps the grep gate green — every entry that
            # DOES land in this list goes through `format_error_entry`.
            started_at=started_at,
            ended_at=ended_at,
        )
        # No extractor call happened; the URL is proven healthy by the
        # persisted events row. Reset failures, leave last_success_at as-is.
        _upsert_after_run(
            conn,
            source_url=source_url,
            now_value=started_at,
            previous_state=state,
            had_extraction_success=False,
            had_error=False,
        )
        append_run_record(record, audit_log_path)
        return record

    if bypass_cadence_gate:
        gate_reason: GateReason = _resolve_gate_reason_for_bypass(state)
        should_run = True
    else:
        should_run, gate_reason = _scheduler_gate(state, cadence, now=now)

    if not should_run:
        ended_at = now()
        record = _empty_record(
            source_url=source_url,
            source_kind=source_kind,
            backend=backend_name,
            gate_reason=gate_reason,
            started_at=started_at,
            ended_at=ended_at,
        )
        # `failure_skip` resets the counter (ADR 0011 §D9) and fires the
        # admin threshold-trigger notification (ADR 0022). `cadence_not_ready`
        # touches nothing — the URL is not due yet.
        if gate_reason == "failure_skip":
            # Capture the pre-reset counter for the notification: this is
            # the value that TRIGGERED the skip (>= CONSECUTIVE_FAILURE_SKIP_THRESHOLD),
            # not the post-reset zero.
            threshold_counter = state.consecutive_failures if state is not None else 0
            _reset_failure_counter_on_gate_skip(conn, source_url=source_url)
            # Rule 4 belt-and-braces: `notify_admins_of_failure_skip` catches
            # every failure surface internally, but wrap here in case a future
            # refactor changes that contract.
            try:
                notify_admins_of_failure_skip(source_url, threshold_counter)
            except Exception as exc:
                logger.warning(
                    "scheduler.notifier: notify_admins_of_failure_skip raised %s",
                    type(exc).__name__,
                )
        append_run_record(record, audit_log_path)
        return record

    # ── Discovery + extraction ────────────────────────────────────────────
    error_entries: list[str] = []
    posts_discovered = 0

    if source_kind == "account":
        if backend_client is None or backend_name is None:
            # Guarded by run_tick's dispatch — if we reach here the caller
            # wired a mis-configured account (no backend client). Treat as
            # a typed error so the audit log carries the signal.
            error_entries.append(
                format_error_entry(
                    "unsupported_source",
                    f"no discovery backend for {backend_name!r}",
                )
            )
            urls_to_extract: list[str] = []
        else:
            try:
                effective_limit = DISCOVERY_LIMIT if discovery_limit is None else discovery_limit
                discovered = backend_client.list_recent_posts(source_url, limit=effective_limit)
                posts_discovered = len(discovered)
                urls_to_extract = list(discovered)
            except (HikerClientError, AnonInstagramClientError) as exc:
                error_entries.append(format_error_entry(exc.error_type, str(exc)))
                urls_to_extract = []
    else:
        urls_to_extract = [source_url]

    if urls_to_extract:
        ok_count, error_count, skipped, extraction_errors = _process_url_extraction(
            conn=conn,
            urls_to_extract=urls_to_extract,
            extractor=extractor,
            system_user_id=system_user_id,
        )
    else:
        ok_count, error_count, skipped = 0, 0, 0
        extraction_errors = []

    error_entries.extend(extraction_errors)

    had_error = bool(error_entries)
    had_extraction_success = ok_count > 0

    _upsert_after_run(
        conn,
        source_url=source_url,
        now_value=started_at,
        previous_state=state,
        had_extraction_success=had_extraction_success,
        had_error=had_error,
    )

    ended_at = now()
    record = SchedulerRunRecord(
        run_id=str(uuid4()),
        source_url=source_url,
        source_kind=source_kind,
        backend=backend_name,
        gate_reason=gate_reason,
        posts_discovered=posts_discovered,
        posts_extracted_ok=ok_count,
        posts_extracted_error=error_count,
        posts_skipped_idempotent=skipped,
        errors=error_entries,
        started_at=started_at,
        ended_at=ended_at,
    )
    append_run_record(record, audit_log_path)
    return record


def run_tick(
    *,
    now: Callable[[], datetime],
    conn_factory: Callable[[], sqlite3.Connection],
    source_by_name: dict[str, EventSource],
    backends: dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol],
    extractor: ExtractorCallable,
    config: SourcesConfig,
    audit_log_path: Path,
) -> TickReport:
    """Run one tick — the pure-composition scheduler entry point.

    Opens exactly one connection through `conn_factory`, bootstraps the
    system user row (idempotent), then iterates every configured post URL
    followed by every configured account URL. Each URL is handed to
    `_process_source_url` with `bypass_cadence_gate=False` — the shared
    work unit does routing, gating, discovery, extraction, audit-log write,
    and `scan_state` update.

    `source_by_name` is reserved for future non-Instagram sources — the
    scheduler is source-agnostic in shape even though only Instagram is
    configured today. Passed through unmodified; the current implementation
    does not consume it (all discovery goes via `backends[account.backend]`).

    `audit_log_path` is the destination for the JSONL run-record stream;
    tests pass a `tmp_path` fixture, the CLI (Stage 4) wires
    `DEFAULT_AUDIT_LOG_PATH`.
    """
    del source_by_name  # reserved for future source dispatch; see docstring.

    started_ns = time.monotonic_ns()
    conn = conn_factory()
    records: list[SchedulerRunRecord] = []
    try:
        system_user = bootstrap_system_user(conn)
        assert system_user.id is not None, "bootstrap_system_user returned an un-persisted row"
        system_user_id = system_user.id

        instagram_config: SourceConfig | None = config.sources.get("instagram")
        if instagram_config is None:
            return TickReport(
                records=[],
                total_events_extracted=0,
                wall_clock_ms=(time.monotonic_ns() - started_ns) // 1_000_000,
            )

        for post_entry in instagram_config.posts:
            record = _process_post_entry(
                conn=conn,
                entry=post_entry,
                source_defaults=instagram_config,
                extractor=extractor,
                now=now,
                audit_log_path=audit_log_path,
                system_user_id=system_user_id,
            )
            records.append(record)

        for account_entry in instagram_config.accounts:
            record = _process_account_entry(
                conn=conn,
                entry=account_entry,
                source_defaults=instagram_config,
                backends=backends,
                extractor=extractor,
                now=now,
                audit_log_path=audit_log_path,
                system_user_id=system_user_id,
            )
            records.append(record)
    finally:
        conn.close()

    total_events = sum(rec.posts_extracted_ok for rec in records)
    wall_clock_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    return TickReport(
        records=records,
        total_events_extracted=total_events,
        wall_clock_ms=wall_clock_ms,
    )


def _process_post_entry(
    *,
    conn: sqlite3.Connection,
    entry: PostConfig,
    source_defaults: SourceConfig,
    extractor: ExtractorCallable,
    now: Callable[[], datetime],
    audit_log_path: Path,
    system_user_id: int,
) -> SchedulerRunRecord:
    """Dispatch one `PostConfig` entry to `_process_source_url`.

    Post URLs skip discovery — `backend_client` and `backend_name` are
    `None`. `_process_source_url` handles the M10 cadence-order swap
    (idempotency pre-check before cadence gate) internally.
    """
    cadence = entry.resolved_cadence(source_defaults)
    return _process_source_url(
        conn=conn,
        source_url=entry.url,
        source_kind="post",
        cadence=cadence,
        backend_client=None,
        backend_name=None,
        extractor=extractor,
        now=now,
        audit_log_path=audit_log_path,
        system_user_id=system_user_id,
        bypass_cadence_gate=False,
    )


def _process_account_entry(
    *,
    conn: sqlite3.Connection,
    entry: AccountConfig,
    source_defaults: SourceConfig,
    backends: dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol],
    extractor: ExtractorCallable,
    now: Callable[[], datetime],
    audit_log_path: Path,
    system_user_id: int,
) -> SchedulerRunRecord:
    """Dispatch one `AccountConfig` entry to `_process_source_url`.

    Account URLs go through discovery via the configured backend before
    the extractor sees any post URL. Cadence-first ordering is preserved
    for accounts (discovery is the rate-limit surface cadence must protect).
    """
    cadence = entry.resolved_cadence(source_defaults)
    backend_client = backends[entry.backend]
    return _process_source_url(
        conn=conn,
        source_url=entry.url,
        source_kind="account",
        cadence=cadence,
        backend_client=backend_client,
        backend_name=entry.backend,
        extractor=extractor,
        now=now,
        audit_log_path=audit_log_path,
        system_user_id=system_user_id,
        bypass_cadence_gate=False,
    )
