"""Open the domain-store database with the v1 schema applied.

One function, `connect()`, is the only way the rest of the tree gets a
`sqlite3.Connection`: it resolves the target from the module-level `DB_PATH`,
creates the file's parent directory when needed, applies
`schema_v1.sql` idempotently, and hands the caller an open connection with
`sqlite3.Row` rows and foreign-key enforcement on.

`DB_PATH` is a module global read inside the function body, mirroring
`tools.tools.CANDIDATES_PATH`: a test monkeypatches it (to `":memory:"` or a
`tmp_path` file) and every subsequent `connect()` picks the new value up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH: str | Path = Path("var/planazo.db")

MEMORY = ":memory:"

_SCHEMA_PATH = Path(__file__).parent / "schema_v1.sql"


def connect() -> sqlite3.Connection:
    """Open `DB_PATH` with the v1 schema applied and return the open connection.

    `DB_PATH` is read fresh from the module global on every call — never bound
    as a default parameter value, which would freeze the path at import time
    and make monkeypatching it silently ineffective. Anything other than
    `":memory:"` is treated as a filesystem path and gets its parent directory
    created first.

    The connection has `row_factory = sqlite3.Row` and `PRAGMA foreign_keys =
    ON`, so a `user_id` with no `users` row raises `sqlite3.IntegrityError`
    rather than writing an orphan row. The caller closes the connection.
    """
    target = DB_PATH
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # `executescript` commits any open transaction, and `PRAGMA foreign_keys`
    # is a no-op inside one, so the pragma goes last.
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
