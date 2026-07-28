"""Pydantic v2 aggregates for the scheduler bounded context.

Three shapes land here:

- `ScanState` — one `scan_state` row per source URL the scheduler has seen.
  Field-for-field mirror of the SQL columns in `planazo/storage/migrations/`.
- `SchedulerRunRecord` — the audit-log line the scheduler appends to
  `var/scheduler_runs.jsonl` on every tick, one line per source URL
  processed. Per [ADR 0011 §D8](../../../../../docs/adr/0011-scheduled-ingestion.md)
  the record has its own schema (not `RunStep`), and per ADR 0014's
  partial-supersede of that decision the grain is per source URL (not per
  account), with the field set extended by `source_kind`, `backend`,
  `started_at`, `ended_at`, and `gate_reason`.
- `TickReport` — the shape `scheduler.service.run_tick` returns, summarising
  a full tick as a list of records plus two aggregate fields the CLI can
  print or a wrapper can compare against.

The `format_error_entry` helper is the ONLY sanctioned way to build entries
for `SchedulerRunRecord.errors`. AGENTS.md rule 2 — scraped text (Instagram
captions, exception messages that quote captions) must never bleed into the
audit log verbatim. The helper truncates + sanitises + regex-locks the
canonical `"<error_type>: <detail>"` shape, and `SchedulerRunRecord` has a
model-level regex validator that fires even when a caller bypasses the
helper. Defense in depth: the helper is the ergonomic front door, the
validator is the boundary lock.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planazo.sources.base import ErrorType

TRUNCATE_LEN: Final[int] = 120
"""Maximum length of the sanitized `detail` portion of an audit-log error entry.

Not an env var: this is a hard product decision (a per-URL error line stays
readable in `tail -f`) rather than a tuning knob. Rule 10 discipline — no
premature configurability.
"""

_ERRORS_ENTRY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(unsupported_source|rate_limited|auth_failed|not_found|unsupported_media): [^\n\t]{0,"
    + str(TRUNCATE_LEN)
    + r"}$"
)
"""Regex `SchedulerRunRecord.errors` entries must match at the model boundary.

Enforced by the after-model validator below so a caller that bypasses
`format_error_entry` still fails at validation. The `error_type` prefix is
one of the five members of `sources.base.ErrorType` verbatim; the detail is
0-120 characters of anything except newline/tab. Anchored on both ends so a
free-form suffix (a caption fragment tacked on the end) cannot slip through.
"""

_INTERNAL_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")

SchedulerBackend = Literal["anonymous", "hikerapi"]
"""The two discovery backends `AccountConfig.backend` routes accounts to.

Mirrors `sources.config.AccountConfig.backend` — the scheduler stores the
resolved backend name on every account-source record for operator
attribution. `None` on post-source records: posts skip discovery.
"""

SourceKind = Literal["post", "account"]
"""Discriminator naming the config block a source URL came from.

`posts:` → `"post"`; `accounts:` → `"account"`. Combined with the `backend`
field it identifies both the entry kind (post vs discovered-post) and the
discovery path (anonymous vs hikerapi) each record was produced under.
"""

GateReason = Literal[
    "first_run",
    "cadence_ready",
    "cadence_not_ready",
    "failure_skip",
]
"""Why the scheduler ran (or didn't run) extraction for a given source URL.

Two "did run" branches: `first_run` (no `scan_state` row yet) and
`cadence_ready` (cadence window elapsed). Two "did not run" branches:
`cadence_not_ready` (still within the cadence window) and `failure_skip`
(consecutive-failure gate tripped, per ADR 0011 §D9). The field is required
so every record commits to one of the four — no `None` sentinel — and
observability keeps the same taxonomy across the tick service, the audit
log, and the reviewer of a live run.
"""


def format_error_entry(error_type: ErrorType, detail: str) -> str:
    """Build a canonical `"<error_type>: <detail>"` entry for `SchedulerRunRecord.errors`.

    This helper is the only sanctioned constructor for audit-log error
    entries. AGENTS.md rule 2 — scraped text (Instagram captions, exception
    messages that quote them) must not bleed into persisted state verbatim.
    The helper:

    1. Rejects an unknown `error_type` at construction time with `ValueError`
       (defensive: `ErrorType` is a `Literal`, so mypy already catches this
       at the type layer; the runtime check catches callers who bypass mypy
       — reflected data, `str(exc.error_type)` from a downstream layer, etc).
    2. Strips newlines, tabs, and any C0/C1 control below `\\x20` from
       `detail`, then collapses runs of remaining whitespace to single
       spaces. Prevents a caption's line break from splitting the entry
       across two JSONL lines.
    3. Truncates the sanitised detail to `TRUNCATE_LEN` characters. Unicode
       code points, not bytes: a truncation across a multi-byte code point
       would produce invalid UTF-8, which is a defect the log reader has to
       catch, so we clip on characters.
    4. Returns the canonical shape `f"{error_type}: {truncated_detail}"`.
       Empty `detail` produces `f"{error_type}: "` — still matches the
       model's regex, still parses.
    """
    if error_type not in get_args(ErrorType):
        raise ValueError(f"unknown error_type: {error_type!r}")

    # Strip control characters (including newline/tab) and collapse remaining
    # whitespace. `chr(c) for c in range(0x20)` covers every C0 control; the
    # DEL (`\x7f`) and the C1 controls (`\x80`-`\x9f`) round it out.
    control_chars = (
        {chr(c) for c in range(0x20)} | {chr(0x7F)} | {chr(c) for c in range(0x80, 0xA0)}
    )
    cleaned = "".join(" " if ch in control_chars else ch for ch in detail)
    cleaned = _INTERNAL_WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    truncated = cleaned[:TRUNCATE_LEN]
    return f"{error_type}: {truncated}"


class ScanState(BaseModel):
    """One `scan_state` row — the scheduler's per-source-URL bookkeeping.

    `source_url` is the primary key: both post entries and account entries
    share this table, and `source_url` is the honest name for both. The
    two `*_at` fields are `None` on a freshly-seeded row (the URL has been
    persisted but never actually scanned); `consecutive_failures` starts at
    zero and never goes negative.
    """

    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1)
    last_scanned_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)


class SchedulerRunRecord(BaseModel):
    """One line the scheduler appends to `var/scheduler_runs.jsonl` per source URL.

    Written under `scheduler.audit.append_run_record` at the end of each URL
    the tick processes; the reader is human (`tail -f`) or a downstream
    aggregator, never the LLM loop. Per ADR 0011 §D8 (partially superseded
    by ADR 0014 §D8 status marker) the grain is one record per source URL,
    the field set covers the four counters + the four attribution fields
    (`run_id`, `source_url`, `source_kind`, `backend`) + the two boundary
    timestamps (`started_at`, `ended_at`) + the observability tag
    (`gate_reason`) + the regex-locked error list.

    Two after-validators run defense-in-depth:

    - `source_kind`/`backend` cross-field check: post-source records must
      carry `backend=None` (posts skip discovery); account-source records
      must carry a populated `backend`.
    - Every entry in `errors` matches `_ERRORS_ENTRY_PATTERN`. Prevents a
      caller who bypasses `format_error_entry` from writing a free-form
      exception string that quotes an Instagram caption verbatim (Rule 2).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_kind: SourceKind
    backend: SchedulerBackend | None = None
    gate_reason: GateReason
    posts_discovered: int = Field(ge=0)
    posts_extracted_ok: int = Field(ge=0)
    posts_extracted_error: int = Field(ge=0)
    posts_skipped_idempotent: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def _validate_backend_matches_source_kind(self) -> SchedulerRunRecord:
        if self.source_kind == "post" and self.backend is not None:
            raise ValueError(
                "SchedulerRunRecord.backend must be None when source_kind='post' "
                "(posts skip discovery)"
            )
        if self.source_kind == "account" and self.backend is None:
            raise ValueError(
                "SchedulerRunRecord.backend must be populated when source_kind='account'"
            )
        return self

    @model_validator(mode="after")
    def _validate_errors_shape(self) -> SchedulerRunRecord:
        for entry in self.errors:
            if not _ERRORS_ENTRY_PATTERN.fullmatch(entry):
                raise ValueError(
                    "SchedulerRunRecord.errors entries must match "
                    f"'{_ERRORS_ENTRY_PATTERN.pattern}' — build them with "
                    "scheduler.models.format_error_entry"
                )
        return self


class TickReport(BaseModel):
    """The return shape of `scheduler.service.run_tick`.

    Aggregates a whole tick as its list of records + two derived totals the
    CLI or a test can compare against without re-summing the JSONL log.
    """

    model_config = ConfigDict(extra="forbid")

    records: list[SchedulerRunRecord] = Field(default_factory=list)
    total_events_extracted: int = Field(ge=0)
    wall_clock_ms: int = Field(ge=0)
