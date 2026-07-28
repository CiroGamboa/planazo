"""Open the domain-store database with the v1 + v2 schema applied.

One function, `connect()`, is the only way the rest of the tree gets a
`sqlite3.Connection`: it resolves the target from the module-level `DB_PATH`,
creates the file's parent directory when needed, applies `schema_v1.sql`
idempotently, applies `schema_v2.sql` exactly once per database (guarded by
`schema_migrations`), and hands the caller an open connection with
`sqlite3.Row` rows and foreign-key enforcement on.

`DB_PATH` is a module global read inside the function body, mirroring
`tools.tools.CANDIDATES_PATH`: a test monkeypatches it (to `":memory:"` or a
`tmp_path` file) and every subsequent `connect()` picks the new value up.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_PATH: str | Path = Path("var/planazo.db")

MEMORY = ":memory:"

_SCHEMA_V1_PATH = Path(__file__).parent / "schema_v1.sql"
_SCHEMA_V2_PATH = Path(__file__).parent / "schema_v2.sql"
_SCHEMA_V2_VERSION = 2


def connect() -> sqlite3.Connection:
    """Open `DB_PATH` with the v1 + v2 schema applied and return the connection.

    `DB_PATH` is read fresh from the module global on every call — never bound
    as a default parameter value, which would freeze the path at import time
    and make monkeypatching it silently ineffective. Anything other than
    `":memory:"` is treated as a filesystem path and gets its parent directory
    created first.

    The connection has `row_factory = sqlite3.Row` and `PRAGMA foreign_keys =
    ON`, so a `user_id` with no `users` row raises `sqlite3.IntegrityError`
    rather than writing an orphan row. If applying `schema_v2.sql` fails
    partway through, the connection is closed (releasing its lock on a
    file-backed database) before the exception propagates, so a fresh
    `connect()` against the same target is unaffected. The caller closes the
    connection on the success path.
    """
    target = DB_PATH
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        # `executescript` commits any open transaction, and `PRAGMA
        # foreign_keys` is a no-op inside one, so both migrations run before
        # the pragma below.
        conn.executescript(_SCHEMA_V1_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        _apply_schema_v2(conn)
    except Exception:
        conn.close()
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _schema_v2_statements() -> list[str]:
    """The five `ALTER TABLE` statements in `schema_v2.sql`, in file order.

    Unlike `schema_v1.sql`'s `CREATE TABLE IF NOT EXISTS` statements, these
    are not independently idempotent, so `_apply_schema_v2` cannot hand the
    whole file to `executescript()` — it needs each statement as its own
    string to run through its own `execute()` call inside one transaction it
    controls.
    """
    lines = (
        line
        for line in _SCHEMA_V2_PATH.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def _apply_schema_v2(conn: sqlite3.Connection) -> None:
    """Apply `schema_v2.sql`'s columns exactly once, atomically.

    Guarded by `schema_migrations`: a database that already has version 2
    recorded is a no-op. Otherwise every `ALTER TABLE` statement and the
    version-recording `INSERT` run inside one `BEGIN` / `commit()` —
    `executescript()` does not roll back earlier DDL in the same script when
    a later statement fails (verified empirically against SQLite 3.45.1), so
    this instead runs each statement through its own `execute()` call and
    rolls back the whole batch on any failure, making "columns added,
    version not recorded" an unreachable state rather than a risk to detect
    after the fact.
    """
    already_applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (_SCHEMA_V2_VERSION,)
    ).fetchone()
    if already_applied is not None:
        return

    conn.execute("BEGIN")
    try:
        for statement in _schema_v2_statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (_SCHEMA_V2_VERSION, datetime.now(UTC).isoformat()),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
