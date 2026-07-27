"""The markdown rules store: operator-editable behaviour, read fresh from disk.

`load_rules()` concatenates the committed `*.md` files under `RULES_DIR` and
caches nothing, so changing what the agent is told is an edit to a markdown file
plus a re-run — no code change, no deploy.

`RULES_DIR` is a module global resolved at call time for the same reason
`storage.db.DB_PATH` is: bound as a default parameter value it would freeze the
path at import time and make monkeypatching it silently ineffective.
"""

from __future__ import annotations

from pathlib import Path

RULES_DIR: Path = Path("data/rules")


def load_rules() -> str:
    """Return every `*.md` file under `RULES_DIR`, sorted by filename, joined.

    Files are separated by one blank line, with each file's surrounding
    whitespace trimmed so the joined text has no ragged gaps. Nothing is cached:
    every call re-reads the directory, so an edit lands on the next call. A
    `RULES_DIR` that is absent or holds no `*.md` files yields `""`.
    """
    directory = RULES_DIR
    if not directory.is_dir():
        return ""
    return "\n\n".join(
        path.read_text(encoding="utf-8").strip() for path in sorted(directory.glob("*.md"))
    )
