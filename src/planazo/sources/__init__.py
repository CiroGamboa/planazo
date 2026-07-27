"""Source-adapter bounded context — the `EventSource` implementations.

Public surface — `models`, `base`, and `config` are the shapes and helpers
every source adapter shares:

- `RawPost`, `MediaAsset` — the payload every `fetch_post` returns on the
  happy path (Pydantic v2; ADR 0006).
- `SOURCES` — the module-level registry the composition root consults;
  monkeypatched in tests.
- `ErrorType`, `error_state` — the typed error taxonomy every adapter
  returns on failure (AGENTS.md rule 4).
- `next_run_after` — deterministic scheduling helper with an injected
  `now` callable.
- `SourcesConfig`, `load_config` — Pydantic-validated `data/sources.yaml`
  loader; fails at boot on a malformed config.

Concrete adapters (`sources/instagram/`, future `sources/tiktok/`) land in
their own subpackages; each conforms structurally to
`planazo.interfaces.sources.EventSource`.
"""

from planazo.sources.base import SOURCES, ErrorType, error_state, next_run_after
from planazo.sources.config import SourcesConfig, load_config
from planazo.sources.models import MediaAsset, RawPost

__all__ = [
    "SOURCES",
    "ErrorType",
    "MediaAsset",
    "RawPost",
    "SourcesConfig",
    "error_state",
    "load_config",
    "next_run_after",
]
