"""Integration tests for the two demo scripts that need no API key.

`untrusted_content.py` has no test here: it calls the real provider, so its
command lives in the ticket's manual validation, next to the live gate tests.
"""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from planazo.memory import facts
from planazo.storage import db

_DEMO_DIR = Path(__file__).resolve().parents[1] / "scripts" / "demo"


@pytest.fixture(autouse=True)
def _restore_store_roots() -> Iterator[None]:
    """Put the store roots back after a demo has redirected them.

    The scripts reassign `facts.MEMORY_ROOT` and `db.DB_PATH` as plain module
    globals — they are scripts, with no `monkeypatch` fixture to unwind them —
    and those modules are shared with the rest of the session, so the redirect
    is undone here rather than inherited by whatever test runs next.
    """
    memory_root, db_path = facts.MEMORY_ROOT, db.DB_PATH
    yield
    facts.MEMORY_ROOT, db.DB_PATH = memory_root, db_path


def _load_demo(name: str) -> ModuleType:
    """Load one demo script from its path.

    The files under `scripts/demo/` are standalone scripts, not a package;
    adding `__init__.py` machinery would present them as an importable library
    they are not. Loading by file location runs them exactly as
    `python scripts/demo/<name>.py` does, minus the `__main__` block.
    """
    spec = importlib.util.spec_from_file_location(name, _DEMO_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_memory_demo_records_the_owner_match_and_the_other_users_miss() -> None:
    markdown = _load_demo("private_memory").main()

    # The owner's recall, with the fact's real content — read back off disk by
    # the script itself, not asserted from a fixture.
    assert 'retrieve_facts(user_id=1, query="music events tonight") -> 1 fact' in markdown
    assert "pays for Spotify Premium" in markdown

    # And the identical query for the other user, reported as empty explicitly:
    # an evidence file that merely omitted the second result would read as a pass.
    assert 'retrieve_facts(user_id=2, query="music events tonight") -> 0 facts' in markdown
    assert "Not found for user 2." in markdown


def test_shared_memory_demo_records_the_other_user_reading_the_authored_note() -> None:
    markdown = _load_demo("shared_memory").main()

    assert 'retrieve_notes(user_id=2, event_id="E-123") -> 1 note' in markdown
    assert "loud venue, arrive early" in markdown
    # The reader sees the author, which is what makes attribution possible.
    assert "authored by user 1" in markdown
