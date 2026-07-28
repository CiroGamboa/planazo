"""Tests for the `storage/db.py` migration runner.

These tests exercise the ticket #87 invariants: a fresh DB is brought all the
way forward, a second `connect()` on the same file is a no-op, migration
order is lexicographic by filename prefix, a mid-migration failure leaves
`user_version` at the last successful step (the load-bearing invariant), a
pre-framework DB with pre-existing tables is safe because the baseline is
`CREATE TABLE IF NOT EXISTS`, and a `user_version` beyond the newest
available migration refuses to connect rather than silently proceeding.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from planazo.storage import db


def _user_version(path: Path) -> int:
    """Open the db outside the runner and read the SQLite version pragma."""
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_fresh_db_applies_every_migration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A brand-new file opens at `user_version == 0` and ends at the newest."""
    dbfile = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)

    assert not dbfile.exists()
    conn = db.connect()
    try:
        migrations = db._discover_migrations(db._MIGRATIONS_DIR)
        expected_version = migrations[-1][0]
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == expected_version
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        # 001_baseline.sql lands these six tables.
        assert {
            "events",
            "users",
            "preferences",
            "approvals",
            "extraction_runs_index",
            "scan_state",
        }.issubset(tables)
    finally:
        conn.close()


def test_second_connect_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Once every migration has landed, opening the file again changes nothing."""
    dbfile = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)

    first = db.connect()
    first.execute(
        "INSERT INTO users(telegram_user_id, display_name, created_at) "
        "VALUES ('tg-1', 'Alice', '2026-01-01T00:00:00+00:00')"
    )
    first.commit()
    version_after_first = int(first.execute("PRAGMA user_version").fetchone()[0])
    first.close()

    second = db.connect()
    try:
        assert int(second.execute("PRAGMA user_version").fetchone()[0]) == version_after_first
        # The row from the first session still exists — idempotent means data
        # is untouched, not just tables.
        row = second.execute(
            "SELECT display_name FROM users WHERE telegram_user_id = 'tg-1'"
        ).fetchone()
        assert row["display_name"] == "Alice"
    finally:
        second.close()


def test_migrations_apply_in_lexicographic_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synthetic top migration lands after every real one, in order.

    We build an isolated migrations directory that mirrors the real one, add
    a synthetic migration that depends on the baseline (`CREATE TABLE ...
    REFERENCES users(id)`) at a version one past the highest real migration,
    and confirm both apply and `user_version` lands on that version. If
    order were wrong the FK reference to `users` would fail.
    """
    real = db._MIGRATIONS_DIR
    fake = tmp_path / "migrations"
    fake.mkdir()
    for path in real.iterdir():
        if path.suffix == ".sql":
            (fake / path.name).write_text(path.read_text(encoding="utf-8"))
    real_migrations = db._discover_migrations(real)
    next_version = real_migrations[-1][0] + 1
    (fake / f"{next_version:03d}_test_migration.sql").write_text(
        "CREATE TABLE test_flag (\n"
        "    user_id INTEGER NOT NULL REFERENCES users(id),\n"
        "    flag    TEXT    NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", fake)

    dbfile = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)

    conn = db.connect()
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == next_version
        # The FK column exists — proves every earlier migration ran first.
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "test_flag" in tables
        assert "users" in tables
    finally:
        conn.close()


def test_mid_migration_failure_leaves_user_version_at_last_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Injected bad SQL in a top migration aborts the whole transaction.

    Load-bearing invariant: after the failure, `user_version` reads back as
    the version of the last successful step — never the failing script's
    version. The DDL that came before the error inside the failing script
    must not have persisted, so a re-run of the same migration on a fixed
    file starts cleanly rather than tripping over half-applied state.
    """
    real = db._MIGRATIONS_DIR
    fake = tmp_path / "migrations"
    fake.mkdir()
    for path in real.iterdir():
        if path.suffix == ".sql":
            (fake / path.name).write_text(path.read_text(encoding="utf-8"))
    real_migrations = db._discover_migrations(real)
    last_good_version = real_migrations[-1][0]
    fail_version = last_good_version + 1
    # Two statements: the first is valid DDL, the second is deliberately
    # malformed. If our transaction wrapper is honest, neither persists and
    # `user_version` stays at the previous version.
    (fake / f"{fail_version:03d}_will_fail.sql").write_text(
        "CREATE TABLE partial_target (id INTEGER PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", fake)

    dbfile = tmp_path / "planazo.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)

    with pytest.raises(sqlite3.OperationalError):
        db.connect()

    # Re-open outside the runner to inspect the raw pragma + tables.
    assert _user_version(dbfile) == last_good_version
    inspect = sqlite3.connect(dbfile)
    try:
        tables = {
            row[0]
            for row in inspect.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        # Every real migration's tables are here; the failing script's
        # `partial_target` must not be.
        assert "users" in tables
        assert "partial_target" not in tables
    finally:
        inspect.close()


def test_pre_framework_db_migrates_cleanly_and_preserves_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DB created before the framework — tables exist, `user_version == 0`.

    We build one by hand (bypassing the runner), insert a row, then call
    `connect()`. Because 001_baseline.sql is `CREATE TABLE IF NOT EXISTS`,
    every statement is a no-op, `user_version` advances to 1, and the row
    survives.
    """
    dbfile = tmp_path / "planazo.db"
    baseline = (db._MIGRATIONS_DIR / "001_baseline.sql").read_text(encoding="utf-8")
    manual = sqlite3.connect(dbfile)
    try:
        manual.executescript(baseline)
        manual.execute(
            "INSERT INTO users(telegram_user_id, display_name, created_at) "
            "VALUES ('tg-legacy', 'Pre-framework', '2025-12-01T00:00:00+00:00')"
        )
        manual.commit()
        assert int(manual.execute("PRAGMA user_version").fetchone()[0]) == 0
    finally:
        manual.close()

    monkeypatch.setattr(db, "DB_PATH", dbfile)
    conn = db.connect()
    try:
        migrations = db._discover_migrations(db._MIGRATIONS_DIR)
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == migrations[-1][0]
        row = conn.execute(
            "SELECT display_name FROM users WHERE telegram_user_id = 'tg-legacy'"
        ).fetchone()
        assert row["display_name"] == "Pre-framework"
    finally:
        conn.close()


def test_user_version_beyond_max_refuses_to_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DB written by a future revision cannot be safely opened by this one.

    We set `user_version = 99` on a fresh file and expect `connect()` to
    raise a `RuntimeError` that names the downgrade constraint. The mention
    of "downgrade" is asserted so a future refactor that swallows the error
    into a generic message trips the test.
    """
    dbfile = tmp_path / "planazo.db"
    prep = sqlite3.connect(dbfile)
    try:
        prep.execute("PRAGMA user_version = 99")
        prep.commit()
    finally:
        prep.close()

    monkeypatch.setattr(db, "DB_PATH", dbfile)
    with pytest.raises(RuntimeError, match="downgrade"):
        db.connect()


def test_bad_migration_filename_is_rejected(tmp_path: Path) -> None:
    """A `.sql` file that does not match `NNN_<name>.sql` is a runner bug.

    We would otherwise silently skip it (or worse, parse the wrong version
    from the filename), so the discover step raises rather than proceeding.
    """
    fake = tmp_path / "migrations"
    fake.mkdir()
    (fake / "not_numbered.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="NNN_"):
        db._discover_migrations(fake)
