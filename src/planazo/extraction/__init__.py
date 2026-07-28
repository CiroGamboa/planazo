"""The `extraction` bounded context — Extractor delegation hand-off + audit log.

Public surface consumed by (a) the Extractor composition root
`agents/extractor.py` (writes `ExtractionRunLogger`, returns `ExtractionResult`),
(b) the Recommender's future `dispatch_extraction` tool (reads
`ExtractionResult`), and (c) the monitor CLI (reads the JSONL file at
`default_extraction_log_path()`).

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
