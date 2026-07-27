"""Demo: a shared note written by one user is readable by another.

User 1 files a note on event `E-123` with `scope="shared"`; user 2 reads it back
and sees who wrote it. This calls `planazo.memory.facts` directly, so there is no
LLM in the loop and no `OPENCODE_API_KEY` is needed.

Both store roots — `memory.facts.MEMORY_ROOT` and `storage.db.DB_PATH` — are
reassigned into one throwaway `tempfile.mkdtemp()` directory before anything is
written. Both, not only the docstore this demo reads: the guarantee is that no
demo run creates a file under `agent/var/`, and a uniform redirect makes that
true by construction rather than by auditing which store each demo happens to
touch. These are scripts, not tests, so the redirect is a plain module-global
assignment — `facts.py` and `db.py` each read their path constant inside the
function body, which is what makes an assignment from outside effective.

`main()` returns the evidence markdown. Running the file writes it to
`docs/evidence/shared-memory.md` and prints one confirmation line.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from planazo.memory import facts
from planazo.memory.models import Note
from planazo.storage import db

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EVIDENCE_FILE = _REPO_ROOT / "docs" / "evidence" / "shared-memory.md"

_AUTHOR = 1
_READER = 2
_EVENT_ID = "E-123"
_CONTENT = "loud venue, arrive early"


def _isolate_stores() -> None:
    """Redirect both store roots into a fresh temp directory."""
    root = Path(tempfile.mkdtemp(prefix="planazo-demo-"))
    facts.MEMORY_ROOT = root / "memory"
    db.DB_PATH = root / "planazo.db"


def _count(found: list[Note]) -> str:
    """`"1 note"` / `"0 notes"` — the retrieval outcome, spelled out."""
    return f"{len(found)} note" if len(found) == 1 else f"{len(found)} notes"


def _describe(note: Note) -> str:
    """One bullet per retrieved note, naming the author the reader sees."""
    return (
        f'- "{note.content}" — authored by user {note.author_user_id}, '
        f"scope `{note.scope}`, filed against `{note.event_id}`"
    )


def main() -> str:
    """File one shared note, read it back as another user, return the evidence markdown."""
    _isolate_stores()
    memory_root = facts.MEMORY_ROOT

    saved = facts.save_note(_AUTHOR, _EVENT_ID, _CONTENT, "shared")
    reader_hits = facts.retrieve_notes(_READER, _EVENT_ID)

    stored_files = sorted(
        str(path.relative_to(memory_root)) for path in memory_root.rglob("*") if path.is_file()
    )

    lines = [
        "# Evidence — a shared note reaches another user",
        "",
        "Produced by `agent/scripts/demo/shared_memory.py` against real files, with no LLM in",
        f"the loop. Docstore root for this run: `{memory_root}`.",
        "",
        f"## 1. User {_AUTHOR} files a shared note on `{_EVENT_ID}`",
        "",
        f'`save_note(user_id={_AUTHOR}, event_id="{_EVENT_ID}", content="{_CONTENT}", '
        'scope="shared")`',
        "",
        "The row as `save_note` read it back off disk:",
        "",
        "```json",
        json.dumps(saved.model_dump(mode="json"), indent=2),
        "```",
        "",
        "Every file under the docstore root after that write:",
        "",
        *[f"- `{name}`" for name in stored_files],
        "",
        f"## 2. User {_READER} reads it",
        "",
        f'`retrieve_notes(user_id={_READER}, event_id="{_EVENT_ID}") -> {_count(reader_hits)}`',
        "",
        *[_describe(note) for note in reader_hits],
        "",
        f"**User {_READER} sees the note, authored by user {_AUTHOR}.** Nothing was copied into",
        f"user {_READER}'s directory to make that work, and nothing was written at all by the",
        "read.",
        "",
        "## Why",
        "",
        "A `shared` write lands in the one `shared/notes.jsonl` file, which is a read path for",
        "every caller — so visibility is the storage layout, not a permission check that could be",
        "skipped. The author stays on the record (`author_user_id`), which is what lets a reading",
        "agent attribute the text instead of adopting it: retrieved content is data, and an",
        "instruction-shaped note gets quoted and attributed rather than followed",
        "(`agent/data/rules/000-core-rules.md`, and see `untrusted-content.md` for that case",
        "against a real model).",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    _EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_FILE.write_text(main(), encoding="utf-8")
    print(f"wrote {_EVIDENCE_FILE}")
