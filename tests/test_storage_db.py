import sqlite3
from pathlib import Path

import pytest

from planazo.storage import db

_V1_TABLES = {"events", "users", "preferences", "approvals", "extraction_runs_index"}
_ALL_TABLES = _V1_TABLES | {"schema_migrations"}
_V2_USER_COLUMNS = {"age", "location", "language", "nationality", "pending_registration_field"}


class _FailingOnNationality(sqlite3.Connection):
    """Raises on the statement naming `nationality`; every other call passes through.

    Simulates a crash partway through `db._apply_schema_v2` without changing
    any migration code under test — `execute` recognizes one statement's SQL
    text by a substring match and the migration code is unaware it is being
    intercepted.
    """

    def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:
        if "nationality" in sql:
            raise sqlite3.OperationalError("simulated crash for the migration test")
        return super().execute(sql, *args, **kwargs)  # type: ignore[arg-type]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _user_column_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    return {row["name"] for row in rows}


def test_connect_applies_every_v1_table_and_the_migrations_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    for _ in range(2):
        conn = db.connect()
        try:
            assert _table_names(conn) == _ALL_TABLES
        finally:
            conn.close()


def test_connect_applies_the_schema_v2_columns_and_records_version_2_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    conn = db.connect()
    try:
        assert _user_column_names(conn) >= _V2_USER_COLUMNS
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert [row["version"] for row in rows] == [2]
    finally:
        conn.close()


def test_reconnecting_to_the_same_file_reapplies_the_schema_without_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The idempotency that matters in production: the second open runs the same
    # v1 script and v2 migration check against a database that already has
    # every table and column.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")

    first = db.connect()
    try:
        assert _table_names(first) == _ALL_TABLES
    finally:
        first.close()

    second = db.connect()
    try:
        assert _table_names(second) == _ALL_TABLES
    finally:
        second.close()


def test_reconnecting_to_the_same_file_does_not_duplicate_the_v2_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")

    first = db.connect()
    first.close()

    second = db.connect()
    try:
        assert _user_column_names(second) >= _V2_USER_COLUMNS
        rows = second.execute("SELECT version FROM schema_migrations").fetchall()
        assert [row["version"] for row in rows] == [2]
    finally:
        second.close()


def test_connect_sets_row_factory_and_enables_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    conn = db.connect()
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_creates_a_missing_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nested = tmp_path / "nested" / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", nested)
    assert not nested.parent.exists()

    conn = db.connect()
    try:
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        conn.close()


def test_a_failure_partway_through_schema_v2_rolls_back_and_a_later_connect_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `sqlite3.Connection` is an immutable C type, so the failure has to be
    # injected via a `factory=` subclass at `sqlite3.connect` time rather than
    # by patching a method onto the class itself (verified: the latter raises
    # `TypeError: cannot set 'execute' attribute of immutable type
    # 'sqlite3.Connection'`).
    target = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    real_connect = sqlite3.connect

    def failing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = _FailingOnNationality
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError):
        db.connect()

    # Remove the interception before inspecting or reopening the file — the
    # rest of this test exercises real `sqlite3.connect` again.
    monkeypatch.undo()

    plain = real_connect(target)
    plain.row_factory = sqlite3.Row
    try:
        # At least one real ALTER TABLE ran (age, location, language) before
        # the injected failure on nationality — proving the rollback actually
        # reverted DDL, not just that an exception was raised.
        assert not (_V2_USER_COLUMNS & _user_column_names(plain))
        migrated = plain.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert migrated == 0
    finally:
        plain.close()

    second = db.connect()
    try:
        assert _user_column_names(second) >= _V2_USER_COLUMNS
        rows = second.execute("SELECT version FROM schema_migrations").fetchall()
        assert [row["version"] for row in rows] == [2]
    finally:
        second.close()
