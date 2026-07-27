"""The catalog bounded context — the persisted event store.

Owns the shared event catalog both agents read and write: the `Event`
aggregate (one row per event) and the `ExtractionRunIndexEntry` pointer
(one row per Instagram-Extraction run), plus the repository primitives that
manipulate both, plus the two LLM tool adapters `save_event` and
`search_events` that expose the store to the agent loop.

Per [ADR 0008](../../../../../docs/adr/0008-domain-driven-module-layout.md)
+ [ADR 0003](../../../../../docs/adr/0003-sqlite-domain-store.md): one
folder per aggregate cluster, with the two-tier `repository.py` /
`tools.py` split preserved.

Cross-ticket contracts pinned by ADR 0003: the `save_event` and
`search_events` public names + signatures + typed error branches stay
byte-for-byte identical (M3's Extractor imports them by name).
"""

from planazo.catalog.models import Event, ExtractionRunIndexEntry
from planazo.catalog.repository import (
    insert_event,
    list_extraction_runs,
    query_events,
    record_extraction_run,
)
from planazo.catalog.tools import save_event, search_events

__all__ = [
    "Event",
    "ExtractionRunIndexEntry",
    "insert_event",
    "list_extraction_runs",
    "query_events",
    "record_extraction_run",
    "save_event",
    "search_events",
]
