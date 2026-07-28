"""JSONL writer for `SchedulerRunRecord` — the scheduler's audit log.

Mirrors `monitor/logging.py`: one JSON object per line, appended, no
buffering across records. The reader is human (`tail -f var/scheduler_runs.jsonl`)
or a downstream aggregator, so the write is atomic per record and the file
is `.gitignore`d under the repo's `var/` rule.

Per ADR 0011 §D8 (partially superseded by ADR 0014's §D8 status marker for
the grain change) the log's shape is one `SchedulerRunRecord` per source
URL processed. The `append_run_record` primitive is the ONLY sanctioned way
to add an entry: a caller building the record has already gone through the
`SchedulerRunRecord` model boundary, so the regex-locked `errors` shape
(Rule 2 leak channel) is guaranteed by the model, not by this writer.
"""

from __future__ import annotations

import json
from pathlib import Path

from planazo.scheduler.models import SchedulerRunRecord

DEFAULT_AUDIT_LOG_PATH: Path = Path("var/scheduler_runs.jsonl")
"""The default location for `SchedulerRunRecord` lines.

`var/` is `.gitignore`d — the file never enters version control. Tests
monkeypatch the destination to a `tmp_path` fixture; the CLI wires this
constant into `run_tick(audit_log_path=...)` at composition time.
"""


def append_run_record(record: SchedulerRunRecord, path: Path) -> None:
    """Append one JSON-serialised `record` line to `path`.

    Creates the parent directory if it does not exist. Uses compact JSON
    separators (`(",", ":")`) to match `monitor/logging.py` and keep each
    line small enough that `tail -f` shows one record per terminal row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record.model_dump(mode="json"), separators=(",", ":")))
        log_file.write("\n")
