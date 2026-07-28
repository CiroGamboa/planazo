"""The scheduler bounded context — periodic ingestion off `sources.yaml`.

Owns the `ScanState` aggregate + `scan_state` table (per-source-URL
bookkeeping), the `SchedulerRunRecord` audit-log shape (one line per source
URL processed each tick), the `TickReport` return shape of the tick
service, the `format_error_entry` Rule-2 leak channel, the repository
primitives that read/upsert `scan_state`, the idempotent
`bootstrap_system_user` seed for the `users.telegram_user_id="system"` row
every scheduler-driven `extract_once` call attributes to, and the
`run_tick` composition-root entry point the CLI (Stage 4) wires up.

Per [ADR 0008](../../../../../docs/adr/0008-domain-driven-module-layout.md)
+ [ADR 0011](../../../../../docs/adr/0011-scheduled-ingestion.md)
+ [ADR 0014](../../../../../docs/adr/0014-instagram-discovery-backends.md):
one folder per aggregate cluster, with the two-tier `repository.py` split
preserved. The CLI (`cli.py`) lands in Stage 4 of the same PR — this
stage lands the data + storage + service tiers.
"""

from planazo.scheduler.audit import DEFAULT_AUDIT_LOG_PATH, append_run_record
from planazo.scheduler.models import (
    TRUNCATE_LEN,
    GateReason,
    ScanState,
    SchedulerBackend,
    SchedulerRunRecord,
    SourceKind,
    TickReport,
    format_error_entry,
)
from planazo.scheduler.repository import (
    SYSTEM_USER_DISPLAY_NAME,
    SYSTEM_USER_TELEGRAM_ID,
    bootstrap_system_user,
    get_scan_state,
    upsert_scan_state,
)
from planazo.scheduler.service import ExtractorCallable, run_tick

__all__ = [
    "DEFAULT_AUDIT_LOG_PATH",
    "SYSTEM_USER_DISPLAY_NAME",
    "SYSTEM_USER_TELEGRAM_ID",
    "TRUNCATE_LEN",
    "ExtractorCallable",
    "GateReason",
    "ScanState",
    "SchedulerBackend",
    "SchedulerRunRecord",
    "SourceKind",
    "TickReport",
    "append_run_record",
    "bootstrap_system_user",
    "format_error_entry",
    "get_scan_state",
    "run_tick",
    "upsert_scan_state",
]
