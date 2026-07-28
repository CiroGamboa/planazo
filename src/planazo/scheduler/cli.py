"""`planazo-scheduler` — host-cron entry point for scheduled ingestion.

Two subcommands, mutually exclusive:

- `--tick` — run one tick over `data/sources.yaml`. Wires the real
  `AnonInstagramClient` + `HikerClient.from_env()` + `extract_once`, opens
  the DB, dispatches to `run_tick`, appends a `SchedulerRunRecord` line to
  `var/scheduler_runs.jsonl` per source URL, closes, exits.
- `--once <url>` — diagnostic one-shot for a single URL. A post URL
  (`/p/<shortcode>/` or `/reel/<shortcode>/`) bypasses the cadence gate
  (`_process_source_url(..., bypass_cadence_gate=True)`) but still runs
  the idempotency pre-check and writes a `SchedulerRunRecord`. An account
  URL is looked up in `sources.yaml` and routed through the same backend
  the tick would use; an unconfigured account URL exits `2` with a typed
  `{"error_type": "unconfigured_account", ...}` line to stdout.

Exit-code taxonomy (fork 5 + M7 of the plan)
--------------------------------------------

- **`0` — tick completed.** Any completed tick returns `0` regardless of
  per-URL outcomes. Operators read `var/scheduler_runs.jsonl` for per-URL
  health; cron treats non-zero as "the tick itself blew up", not "a URL
  failed" — per-URL failures are expected in steady state and should not
  page the operator. Matches the `docker compose up sources-instagram`
  one-shot discipline (exit `0` on completed dry-run, non-zero only on
  config-load failure).
- **`1` — uncaught exception.** Any exception that escapes `run_tick` —
  schema-validation failure on the record boundary, DB corruption,
  filesystem unwritable, extractor blow-up. One-liner to stderr with the
  exception class + a 120-char-truncated message (Rule 2 leak-channel
  discipline extends to the exception-hoist path; a caption fragment in
  `str(exc)` is exactly what the truncation prevents).
- **`2` — configuration-time failure.** `load_config()` `ValidationError`
  or `FileNotFoundError`, `HikerClient.from_env()` `RuntimeError` (no
  keys configured for a `hikerapi`-backed account), missing
  `sources.instagram` block. Distinct exit code because cron config
  errors and runtime failures need different operator responses — a
  wrapper can alert on `exit == 2` immediately, and treat `exit == 0`
  with `posts_extracted_error > 0` records as a soft signal.

Extractor wiring (M11 of the plan)
----------------------------------

`extract_once` is keyword-only on `delegator_user_id`; `ExtractorCallable`
is positional. The composition root normalises the two by wrapping
`extract_once` in a `_default_extractor(url, uid)` helper. Tests inject a
fake extractor by monkeypatching `_build_extractor`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from planazo.agents.extractor import extract_once
from planazo.extraction.models import ExtractionResult
from planazo.scheduler.audit import DEFAULT_AUDIT_LOG_PATH
from planazo.scheduler.models import SchedulerBackend, SchedulerRunRecord, TickReport
from planazo.scheduler.repository import bootstrap_system_user
from planazo.scheduler.service import (
    ExtractorCallable,
    _process_source_url,
    run_tick,
)
from planazo.sources.config import (
    AccountConfig,
    SourceConfig,
    SourcesConfig,
    is_instagram_post_url,
    load_config,
)
from planazo.sources.instagram.anon_client import AnonInstagramClient
from planazo.sources.instagram.discovery import InstagramDiscoveryProtocol
from planazo.sources.instagram.hiker_client import HikerClient
from planazo.storage import db

__all__ = ["main"]

EXIT_OK: Final[int] = 0
EXIT_UNCAUGHT: Final[int] = 1
EXIT_CONFIG: Final[int] = 2

_EXCEPTION_MESSAGE_TRUNCATE: Final[int] = 120
"""Max length of `str(exc)` on the exit-`1` stderr line — Rule 2 discipline."""

_ZERO_CADENCE: Final[timedelta] = timedelta(0)
"""Placeholder cadence for `--once` invocations.

`--once` always bypasses the cadence gate, so `cadence` is never read on
the code path. Passing `timedelta(0)` keeps the signature honest and does
not construct a `None` sentinel `_process_source_url` would have to check
in an otherwise-typed contract.
"""


# -----------------------------------------------------------------------------
# Composition seams — tests monkeypatch these to inject fakes.
# -----------------------------------------------------------------------------


def _now() -> datetime:
    """Current time in UTC. Injected into `run_tick` and `_process_source_url`."""
    return datetime.now(UTC)


def _conn_factory() -> sqlite3.Connection:
    """Open a connection to the domain-store DB. Closed by `run_tick`'s `finally`."""
    return db.connect()


def _default_extractor(url: str, delegator_user_id: int) -> ExtractionResult:
    """Positional-shape wrapper around `extract_once` — the M11 normalisation.

    `ExtractorCallable = Callable[[str, int], ExtractionResult]` is positional
    by design (see `scheduler.service`); `extract_once` is keyword-only on
    `delegator_user_id`. This function is the one-line bridge the composition
    root wires so downstream tests can substitute a fake by monkeypatching
    `_build_extractor` and never touch the real LLM path.
    """
    return extract_once(url, delegator_user_id=delegator_user_id)


def _build_extractor() -> ExtractorCallable:
    """Return the production extractor callable. Overridable in tests."""
    return _default_extractor


def _build_backends(
    config: SourcesConfig,
) -> dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol]:
    """Construct the discovery-backend registry the tick service dispatches over.

    `AnonInstagramClient` is cheap to construct so it lands unconditionally.
    `HikerClient.from_env()` reads the multi-key pool from the environment and
    raises `RuntimeError` when no keys are set; the caller wraps this
    invocation in the exit-`2` config-error path so a missing key surfaces as
    a distinct exit code (rather than a runtime `KeyError` deep inside
    `run_tick`). If no `hikerapi`-backed account is configured, the `hikerapi`
    slot gets a stub that fails loudly if invoked — the tick service should
    never reach it in that shape.
    """
    backends: dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol] = {
        "anonymous": AnonInstagramClient(),
    }
    if _config_uses_hikerapi(config):
        backends["hikerapi"] = HikerClient.from_env()
    else:
        backends["hikerapi"] = _UnconfiguredHikerapiBackend()
    return backends


def _config_uses_hikerapi(config: SourcesConfig) -> bool:
    """`True` when at least one configured account routes through `hikerapi`."""
    instagram = config.sources.get("instagram")
    if instagram is None:
        return False
    return any(account.backend == "hikerapi" for account in instagram.accounts)


class _UnconfiguredHikerapiBackend:
    """Stand-in for the `hikerapi` slot when no account uses it.

    Raising in `list_recent_posts` here is defensive: the tick service dispatches
    by `AccountConfig.backend`, so an `_UnconfiguredHikerapiBackend` is only
    reached if a config change routes an account through `hikerapi` after the
    process started. A loud runtime `RuntimeError` is preferred over a silent
    `KeyError` because the exit-`2` config-error path already covered the
    boot-time case.
    """

    def list_recent_posts(self, account_url: str, limit: int = 12) -> list[str]:
        raise RuntimeError(
            f"hikerapi backend not initialised — no PLANAZO_IG_HIKER_API_KEY_* "
            f"env var set at boot but a config change routes {account_url!r} "
            f"through hikerapi. Set the env vars and re-run."
        )


# -----------------------------------------------------------------------------
# argparse
# -----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planazo-scheduler",
        description=(
            "Host-cron entry point for scheduled Instagram ingestion. "
            "Reads data/sources.yaml, iterates configured posts and accounts, "
            "and appends one SchedulerRunRecord line per source URL to "
            "var/scheduler_runs.jsonl."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--tick",
        action="store_true",
        help="Run one tick over data/sources.yaml. Exit 0 on completion, 2 on "
        "config failure, 1 on uncaught exception.",
    )
    group.add_argument(
        "--once",
        metavar="URL",
        help="Diagnostic one-shot for a single URL. A /p/ or /reel/ URL bypasses "
        "the cadence gate; an account URL is looked up in sources.yaml and "
        "routed through its configured backend. An unconfigured account URL "
        "exits 2.",
    )
    return parser


# -----------------------------------------------------------------------------
# Command bodies
# -----------------------------------------------------------------------------


def _load_config_for_cli() -> SourcesConfig:
    """Wrap `load_config()` so tests can monkeypatch a single seam.

    Tests inject a `SourcesConfig` directly by monkeypatching this function;
    production reads `data/sources.yaml` verbatim.
    """
    return load_config()


def _run_tick(config: SourcesConfig, *, audit_log_path: Path) -> int:
    """Execute `--tick` end-to-end. Returns the CLI exit code.

    A completed tick — even one where every URL failed — returns `EXIT_OK`
    (per-URL health lives in the JSONL log). Config-time failures land in
    `main`'s `except` blocks and return `EXIT_CONFIG`. Uncaught exceptions
    escape to `main`'s catch-all and return `EXIT_UNCAUGHT`.
    """
    backends = _build_backends(config)
    extractor = _build_extractor()

    report: TickReport = run_tick(
        now=_now,
        conn_factory=_conn_factory,
        source_by_name={},
        backends=backends,
        extractor=extractor,
        config=config,
        audit_log_path=audit_log_path,
    )
    del report  # audit log is the artifact; report is unused by the CLI
    return EXIT_OK


def _find_configured_account(
    config: SourcesConfig, account_url: str
) -> tuple[AccountConfig, SourceConfig] | None:
    """Return the `(AccountConfig, SourceConfig)` matching `account_url`, or `None`.

    Exact URL match on `AccountConfig.url` — the config file is the source
    of truth for backend routing, per Fork 5's rejection of a `--backend`
    override on the CLI.
    """
    for source in config.sources.values():
        for account in source.accounts:
            if account.url == account_url:
                return account, source
    return None


def _run_once(config: SourcesConfig, url: str, *, audit_log_path: Path) -> int:
    """Execute `--once <url>` end-to-end. Returns the CLI exit code.

    Post URLs (`/p/<shortcode>/`, `/reel/<shortcode>/`) route straight into
    `_process_source_url(source_kind="post", bypass_cadence_gate=True)`; the
    idempotency pre-check still applies (Rule 4 discipline — re-extraction of
    a persisted URL burns STRONG-tier LLM budget for nothing).

    Account URLs are looked up in `sources.yaml`; an unconfigured URL exits
    `EXIT_CONFIG` with a typed error line — the config is the source of truth
    for backend routing, and inventing a default backend here would create a
    routing ambiguity between the CLI and the tick service (Fork 5).
    """
    extractor = _build_extractor()

    if is_instagram_post_url(url):
        record = _run_once_post(url, extractor, audit_log_path)
        _print_record(record)
        return EXIT_OK

    match = _find_configured_account(config, url)
    if match is None:
        _print_unconfigured_account_error(url)
        return EXIT_CONFIG
    account, source = match

    backends = _build_backends(config)
    record = _run_once_account(
        account=account,
        source=source,
        backends=backends,
        extractor=extractor,
        audit_log_path=audit_log_path,
    )
    _print_record(record)
    return EXIT_OK


def _run_once_post(
    url: str,
    extractor: ExtractorCallable,
    audit_log_path: Path,
) -> SchedulerRunRecord:
    """Diagnostic single-post invocation of `_process_source_url`.

    Opens one connection (mirroring `run_tick`'s composition), bootstraps
    the system user, and returns the shared work unit's result. Cadence
    bypass is on; idempotency pre-check is still enforced by the shared
    helper (M8 handoff — `--once` and `--tick` share every seam except
    cadence gating).
    """
    conn = _conn_factory()
    try:
        system_user = bootstrap_system_user(conn)
        assert system_user.id is not None, "bootstrap_system_user returned un-persisted row"
        return _process_source_url(
            conn=conn,
            source_url=url,
            source_kind="post",
            cadence=_ZERO_CADENCE,  # unused: bypass_cadence_gate=True + post idempotency-first
            backend_client=None,
            backend_name=None,
            extractor=extractor,
            now=_now,
            audit_log_path=audit_log_path,
            system_user_id=system_user.id,
            bypass_cadence_gate=True,
        )
    finally:
        conn.close()


def _run_once_account(
    *,
    account: AccountConfig,
    source: SourceConfig,
    backends: dict[Literal["anonymous", "hikerapi"], InstagramDiscoveryProtocol],
    extractor: ExtractorCallable,
    audit_log_path: Path,
) -> SchedulerRunRecord:
    """Diagnostic single-account invocation of `_process_source_url`."""
    conn = _conn_factory()
    try:
        system_user = bootstrap_system_user(conn)
        assert system_user.id is not None, "bootstrap_system_user returned un-persisted row"
        backend_name: SchedulerBackend = account.backend
        return _process_source_url(
            conn=conn,
            source_url=account.url,
            source_kind="account",
            cadence=account.resolved_cadence(source),
            backend_client=backends[backend_name],
            backend_name=backend_name,
            extractor=extractor,
            now=_now,
            audit_log_path=audit_log_path,
            system_user_id=system_user.id,
            bypass_cadence_gate=True,
        )
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------


def _print_record(record: SchedulerRunRecord) -> None:
    """Emit the record as one JSON line to stdout for the operator to pipe / grep."""
    print(record.model_dump_json())


def _print_unconfigured_account_error(url: str) -> None:
    """Emit the typed `unconfigured_account` error line to stdout, exit 2 caller side."""
    payload = {
        "error_type": "unconfigured_account",
        "message": (
            f"account URL {url!r} is not in data/sources.yaml. Add it under "
            "sources.instagram.accounts: with an explicit backend, then re-run."
        ),
        "url": url,
    }
    print(json.dumps(payload))


def _print_config_error(exc: Exception) -> None:
    """Emit a typed config-error line to stderr and truncate the exception detail."""
    detail = _truncate_exception_message(str(exc))
    print(
        f"planazo-scheduler: config error ({type(exc).__name__}): {detail}",
        file=sys.stderr,
    )


def _print_uncaught_error(exc: BaseException) -> None:
    """Emit the exit-`1` one-liner to stderr, truncated per Rule 2 discipline."""
    detail = _truncate_exception_message(str(exc))
    print(
        f"planazo-scheduler: uncaught {type(exc).__name__}: {detail}",
        file=sys.stderr,
    )


def _truncate_exception_message(message: str) -> str:
    """Truncate `str(exc)` at `_EXCEPTION_MESSAGE_TRUNCATE` chars — no newlines.

    An exception message can legally embed a caption fragment (a
    `HikerClientError` around a Meta 400 body, an SQLite error naming a
    row); the truncate + strip keeps a stray caption from bleeding to
    stderr where a shell log would pick it up unbounded.
    """
    single_line = message.replace("\n", " ").replace("\t", " ")
    return single_line[:_EXCEPTION_MESSAGE_TRUNCATE]


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------


def _dispatch(args: argparse.Namespace, *, audit_log_path: Path) -> Callable[[], int]:
    """Return the parameterless command runner named by `args`.

    Extracting this dispatch step means `main`'s exception handling wraps
    exactly the command body, not the argparse call — a `SystemExit(2)`
    from argparse propagates through unmodified (`--tick` and `--once`
    mutually-exclusive violation, `--tick` missing, etc.).
    """
    if args.tick:

        def _tick() -> int:
            config = _load_config_for_cli()
            return _run_tick(config, audit_log_path=audit_log_path)

        return _tick

    def _once() -> int:
        config = _load_config_for_cli()
        assert args.once is not None
        return _run_once(config, args.once, audit_log_path=audit_log_path)

    return _once


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point wired via `pyproject.toml` `[project.scripts]`.

    See the module docstring for the exit-code taxonomy. `argv=None` reads
    `sys.argv[1:]`; tests inject a list.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    audit_log_path = DEFAULT_AUDIT_LOG_PATH
    runner = _dispatch(args, audit_log_path=audit_log_path)

    try:
        return runner()
    except FileNotFoundError as exc:
        _print_config_error(exc)
        return EXIT_CONFIG
    except ValidationError as exc:
        _print_config_error(exc)
        return EXIT_CONFIG
    except RuntimeError as exc:
        # `HikerClient.from_env()` raises `RuntimeError` on an empty key pool;
        # that's a config-time failure (exit 2). A `RuntimeError` from
        # `bootstrap_system_user` or a deeper primitive would also route here
        # — acceptable: both fail-fast on missing setup.
        _print_config_error(exc)
        return EXIT_CONFIG
    except Exception as exc:
        _print_uncaught_error(exc)
        return EXIT_UNCAUGHT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
