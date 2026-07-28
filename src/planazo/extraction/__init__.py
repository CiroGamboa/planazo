"""The `extraction` bounded context — Extractor delegation hand-off + audit log.

Public surface consumed by (a) the Extractor composition root
`agents/extractor.py` (writes `ExtractionRunLogger`, returns `ExtractionResult`),
(b) the Recommender's `dispatch_extraction` tool built by
`extraction.tools.build_dispatch_extraction` (reads `ExtractionResult`), and
(c) the monitor CLI (reads the JSONL file at
`default_extraction_log_path()`).

`build_dispatch_extraction` deliberately is NOT re-exported here — callers
that want it import from `planazo.extraction.tools` directly. Re-exporting
would force `tools.py` to load at package-import time, and `tools.py`
top-imports `planazo.agents.extractor` (the Extractor's composition root),
which in turn imports from `planazo.extraction.audit` / `.models`. That is a
circular import at package init. The lazy `from planazo.extraction.tools
import build_dispatch_extraction` inside `event_agent.run_once`'s
`if user_id is not None:` block is the deliberate seam.

Per [ADR 0008](../../../../docs/adr/0008-domain-driven-module-layout.md) each
aggregate lives in its own bounded context; ADR 0005 records why the
delegation hand-off belongs here rather than colocated with
`agents/loop.py::LoopResult` (a transient dataclass) or `catalog/models.py`
(catalog owns the event store, not the cross-agent hand-off surface).
"""

from planazo.extraction.audit import ExtractionRunLogger, default_extraction_log_path
from planazo.extraction.models import (
    ExtractionErrorType,
    ExtractionResult,
    ExtractionStatus,
)

__all__ = [
    "ExtractionErrorType",
    "ExtractionResult",
    "ExtractionRunLogger",
    "ExtractionStatus",
    "default_extraction_log_path",
]
