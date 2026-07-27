"""JSON docstore for facts (cued) and notes (event-scoped), scoped by directory.

Layout under `MEMORY_ROOT`: a caller's own facts and notes live in
`private/{user_id}/{facts,notes}.jsonl`, everything readable by everyone lives
in `shared/{facts,notes}.jsonl`. `scope` is the only branch — there is no third
path, and no parameter in this module names another user's directory.

`user_id` reaches a path only after a `MemoryScopeRequest` has validated it as
an integer, and the *validated* value is what builds the path. A
traversal-shaped id (`"1/../2"`, which the filesystem resolves into user 2's
private directory) is a `ValidationError` rather than a directory. The
`ValidationError` propagates: `memory.api`'s tool wrappers turn it into a typed
error state, while a direct library caller sees the exception, which is the
right branch for a programming error.

`MEMORY_ROOT` is a module global resolved at call time, mirroring
`storage.db.DB_PATH` and `tools.tools.CANDIDATES_PATH`: binding it as a default
parameter value would freeze the path at import time and make monkeypatching it
silently ineffective.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from planazo.memory.models import Fact, MemoryScopeRequest, Note, ReadScope, Scope

MEMORY_ROOT: Path = Path("var/memory")

_FACTS_FILE = "facts.jsonl"
_NOTES_FILE = "notes.jsonl"
_TOKEN = re.compile(r"\w+")


# --------------------------------------------------------------------------
# Paths and JSONL I/O.
# --------------------------------------------------------------------------


def _write_path(user_id: int, scope: Scope, filename: str) -> Path:
    """The single file a write with `scope` lands in."""
    if scope == "private":
        return MEMORY_ROOT / "private" / str(user_id) / filename
    return MEMORY_ROOT / "shared" / filename


def _read_paths(user_id: int, scope: ReadScope, filename: str) -> list[Path]:
    """The files a read with `scope` may touch, in read order.

    `"both"` unions the caller's own private file with the shared one; the two
    narrower scopes pick exactly one of them. A file that does not exist yet is
    still returned — `_read_rows` treats it as empty.
    """
    private = MEMORY_ROOT / "private" / str(user_id) / filename
    shared = MEMORY_ROOT / "shared" / filename
    if scope == "private":
        return [private]
    if scope == "shared":
        return [shared]
    return [private, shared]


def _read_rows(path: Path) -> list[dict[str, object]]:
    """Every JSON object in `path`, or `[]` when the file does not exist."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _append_and_read_back(path: Path, payload: dict[str, object]) -> dict[str, object]:
    """Append `payload` as one JSONL row and return that row as read off disk.

    ADR 0002's write-then-verify pattern: what comes back is what the file now
    holds, so a write that did not land is a failure here rather than a success
    the caller believes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")

    persisted = [row for row in _read_rows(path) if row == payload]
    if not persisted:
        raise OSError(f"append to {path} did not land on disk")
    return persisted[-1]


def _tokens(text: str) -> set[str]:
    """Lowercased `\\w+` tokens of `text` — the unit cue matching compares."""
    return set(_TOKEN.findall(text.lower()))


# --------------------------------------------------------------------------
# Facts: cued, retrieved by token overlap.
# --------------------------------------------------------------------------


def save_fact(user_id: int, cue: str, content: str, scope: Scope) -> Fact:
    """Append one fact for `user_id` and return it as stored.

    The `MemoryScopeRequest` and the `Fact` are both built before any path
    exists, so an invalid `user_id` or an out-of-range `scope` fails without
    locating — let alone creating — a file.
    """
    request = MemoryScopeRequest(user_id=user_id, scope=scope)
    fact = Fact(
        author_user_id=request.user_id,
        cue=cue,
        content=content,
        scope=scope,
        created_at=datetime.now(UTC),
    )
    path = _write_path(request.user_id, fact.scope, _FACTS_FILE)
    return Fact.model_validate(_append_and_read_back(path, fact.model_dump(mode="json")))


def retrieve_facts(user_id: int, query: str, scope: ReadScope = "both") -> list[Fact]:
    """Return the facts in `scope` whose cue shares at least one token with `query`.

    Token overlap only — lowercase, `\\w+`-tokenize both sides, match on a
    non-empty intersection. No ranking and no embeddings; replacing this needs
    its own ADR per ADR 0004.
    """
    request = MemoryScopeRequest(user_id=user_id, scope=scope)
    wanted = _tokens(query)

    found: list[Fact] = []
    for path in _read_paths(request.user_id, request.scope, _FACTS_FILE):
        for row in _read_rows(path):
            fact = Fact.model_validate(row)
            if wanted & _tokens(fact.cue):
                found.append(fact)
    return found


# --------------------------------------------------------------------------
# Notes: event-scoped, retrieved by exact event id.
# --------------------------------------------------------------------------


def save_note(user_id: int, event_id: str, content: str, scope: Scope) -> Note:
    """Append one note about `event_id` for `user_id` and return it as stored."""
    request = MemoryScopeRequest(user_id=user_id, scope=scope)
    note = Note(
        author_user_id=request.user_id,
        event_id=event_id,
        content=content,
        scope=scope,
        created_at=datetime.now(UTC),
    )
    path = _write_path(request.user_id, note.scope, _NOTES_FILE)
    return Note.model_validate(_append_and_read_back(path, note.model_dump(mode="json")))


def retrieve_notes(user_id: int, event_id: str, scope: ReadScope = "both") -> list[Note]:
    """Return the notes in `scope` attached to exactly `event_id`."""
    request = MemoryScopeRequest(user_id=user_id, scope=scope)

    found: list[Note] = []
    for path in _read_paths(request.user_id, request.scope, _NOTES_FILE):
        for row in _read_rows(path):
            note = Note.model_validate(row)
            if note.event_id == event_id:
                found.append(note)
    return found
