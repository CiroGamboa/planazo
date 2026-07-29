"""Open the domain-store database and bring it to the latest schema version.

One function, `connect()`, is the only way the rest of the tree gets a
`sqlite3.Connection`: it resolves the target from the module-level `DB_PATH`,
creates the file's parent directory when needed, runs any pending migration
files from `migrations/` in lexicographic order, and hands the caller an open
connection with `sqlite3.Row` rows and foreign-key enforcement on.

Migrations are versioned by SQLite's `PRAGMA user_version`. Each `NNN_*.sql`
file under `migrations/` declares its version via the numeric prefix; the
runner applies every file whose version is greater than the current
`user_version`, wrapping each in `BEGIN; ... COMMIT;` together with the
matching `PRAGMA user_version = <N>` update so a mid-migration failure leaves
the database at the last successful version rather than a half-applied one.

`DB_PATH` is a module global read inside the function body, mirroring
`tools.tools.CANDIDATES_PATH`: a test monkeypatches it (to `":memory:"` or a
`tmp_path` file) and every subsequent `connect()` picks the new value up.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DB_PATH: str | Path = Path("var/planazo.db")

MEMORY = ":memory:"

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_[A-Za-z0-9_]+\.sql$")


def _discover_migrations(directory: Path) -> list[tuple[int, Path]]:
    """Return `(version, path)` pairs for every migration file in `directory`.

    Files are sorted by their filesystem name (lexicographic order — matching
    the `001_`, `002_`, ... prefix convention). Any `.sql` file whose name does
    not match `NNN_<name>.sql` is a bug that would otherwise silently skip the
    file, so we refuse to run.
    """
    pairs: list[tuple[int, Path]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            raise RuntimeError(f"migration file {path.name!r} does not match NNN_<name>.sql")
        pairs.append((int(match.group(1)), path))
    return pairs


def _apply_migration(conn: sqlite3.Connection, version: int, path: Path) -> None:
    """Apply one migration in a single transaction that also bumps `user_version`.

    `executescript` will `COMMIT` any pending transaction before running, so we
    embed the `BEGIN`/`COMMIT` inside the script itself along with the
    `PRAGMA user_version` update. If any statement in the script raises, the
    whole transaction rolls back and `user_version` stays at its previous
    value — this is the load-bearing invariant that makes a mid-migration
    crash safe.
    """
    sql = path.read_text(encoding="utf-8")
    conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")


def connect() -> sqlite3.Connection:
    """Open `DB_PATH`, apply pending migrations, and return the open connection.

    `DB_PATH` is read fresh from the module global on every call — never bound
    as a default parameter value, which would freeze the path at import time
    and make monkeypatching it silently ineffective. Anything other than
    `":memory:"` is treated as a filesystem path and gets its parent directory
    created first.

    The connection has `row_factory = sqlite3.Row` and `PRAGMA foreign_keys =
    ON`, so a `user_id` with no `users` row raises `sqlite3.IntegrityError`
    rather than writing an orphan row. The caller closes the connection.

    A `user_version` greater than the newest available migration means the
    database was written by a future version of the code: down-migrations are
    out of scope, so we refuse to connect rather than silently proceed against
    a schema we cannot describe.
    """
    target = DB_PATH
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row

    migrations = _discover_migrations(_MIGRATIONS_DIR)
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    max_version = migrations[-1][0] if migrations else 0
    if current_version > max_version:
        conn.close()
        raise RuntimeError(
            f"db user_version {current_version} exceeds available migration "
            f"{max_version}; downgrade not supported"
        )

    for version, path in migrations:
        if version > current_version:
            _apply_migration(conn, version, path)

    # `PRAGMA foreign_keys` is a no-op inside a transaction, so the pragma
    # goes last — after every migration has committed.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
