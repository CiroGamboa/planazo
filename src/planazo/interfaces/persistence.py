"""The per-aggregate persistence contract.

Swap axis: SQLite (today, via `sqlite3.Connection` + hand-rolled SQL) →
Postgres (later, likely via `psycopg`). The Protocol keeps the two tiers
ADR 0003 established: connection-parameterized primitives (which raise
`IntegrityError` on constraint violation), and LLM-facing tool wrappers
(which own their own connection and return typed error dicts).

The Protocol here is deliberately narrow — it declares the *primitive tier*
shape every repository must offer. LLM tool wrappers (`save_event`,
`search_events`, ...) are separate concerns per bounded context; the
protocol does not attempt to unify them.

Downstream (M4's ranker + a future Postgres port) types callsites against
`Repository[T]` for the aggregate type they consume.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol, TypeVar

# `T` is the aggregate type — `Event`, `UserRecord`, `ApprovalDecision`, ...
# Each repository is generic in the one aggregate it owns; cross-aggregate
# queries are a design smell (see ADR 0003) and don't get a Protocol slot.
T = TypeVar("T")


class Repository(Protocol[T]):
    """The primitive-tier persistence contract for one aggregate `T`.

    Every conforming repository (`planazo.catalog.repository`,
    `planazo.identity.repository`, `planazo.approval.repository`) exposes
    connection-parameterized primitives that mirror this shape. A future
    Postgres implementation swaps `sqlite3.Connection` for a
    DB-API 2.0-compatible connection and preserves the same call shape.

    A `sqlite3.IntegrityError` propagates from primitives — no LLM tool
    reaches them, only composition code and tests. LLM-facing tools live
    per-context (`catalog/tools.py` etc.) and turn integrity errors into
    typed branches like `duplicate_event`.
    """

    def insert(self, conn: sqlite3.Connection, aggregate: T) -> int:
        """Insert `aggregate` and return its new row id."""
        ...

    def by_id(self, conn: sqlite3.Connection, row_id: int) -> T | None:
        """Return the row with `row_id`, or `None` if absent."""
        ...
