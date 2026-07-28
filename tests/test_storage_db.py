import sqlite3
from pathlib import Path

import pytest

from planazo.storage import db

_EXPECTED_TABLES = {
    "events",
    "users",
    "preferences",
    "approvals",
    "extraction_runs_index",
    "scan_state",
    "agent_runs",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_connect_applies_every_expected_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", ":memory:")

    for _ in range(2):
        conn = db.connect()
        try:
            assert _table_names(conn) == _EXPECTED_TABLES
        finally:
            conn.close()


def test_reconnecting_to_the_same_file_reapplies_the_schema_without_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The idempotency that matters in production: the second open runs the same
    # CREATE TABLE script against a database that already has every table.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planazo.db")

    first = db.connect()
    try:
        assert _table_names(first) == _EXPECTED_TABLES
    finally:
        first.close()

    second = db.connect()
    try:
        assert _table_names(second) == _EXPECTED_TABLES
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
