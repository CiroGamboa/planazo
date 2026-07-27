"""Demo: a private fact is readable by its owner and by nobody else.

User 1 saves one private fact, then the same cue query runs twice — once as user
1, once as user 2. The owner gets the fact back; the other user gets an empty
list, off the same files on disk. This calls `planazo.memory.facts` directly, so
there is no LLM in the loop and no `OPENCODE_API_KEY` is needed.

Both store roots — `memory.facts.MEMORY_ROOT` and `storage.db.DB_PATH` — are
reassigned into one throwaway `tempfile.mkdtemp()` directory before anything is
written. Both, not only the docstore this demo reads: the guarantee is that no
demo run creates a file under `var/`, and a uniform redirect makes that
true by construction rather than by auditing which store each demo happens to
touch. These are scripts, not tests, so the redirect is a plain module-global
assignment — `facts.py` and `db.py` each read their path constant inside the
function body, which is what makes an assignment from outside effective.

`main()` returns the evidence markdown. Running the file writes it to
`docs/evidence/private-memory.md` and prints one confirmation line.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from planazo.memory import facts
from planazo.schemas.memory import Fact
from planazo.storage import db

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EVIDENCE_FILE = _REPO_ROOT / "docs" / "evidence" / "private-memory.md"

_OWNER = 1
_OTHER = 2
_CUE = "music, subscriptions"
_CONTENT = "pays for Spotify Premium"
_QUERY = "music events tonight"


def _isolate_stores() -> None:
    """Redirect both store roots into a fresh temp directory."""
    root = Path(tempfile.mkdtemp(prefix="planazo-demo-"))
    facts.MEMORY_ROOT = root / "memory"
    db.DB_PATH = root / "planazo.db"


def _count(found: list[Fact]) -> str:
    """`"1 fact"` / `"0 facts"` — the retrieval outcome, spelled out."""
    return f"{len(found)} fact" if len(found) == 1 else f"{len(found)} facts"


def _describe(fact: Fact) -> str:
    """One bullet per retrieved fact, carrying its scope and its author."""
    return (
        f'- "{fact.content}" (cue `{fact.cue}`, scope `{fact.scope}`, '
        f"author user {fact.author_user_id})"
    )


def main() -> str:
    """Save one private fact, query it as both users, return the evidence markdown."""
    _isolate_stores()
    memory_root = facts.MEMORY_ROOT

    saved = facts.save_fact(_OWNER, _CUE, _CONTENT, "private")
    owner_hits = facts.retrieve_facts(_OWNER, _QUERY)
    other_hits = facts.retrieve_facts(_OTHER, _QUERY)

    stored_files = sorted(
        str(path.relative_to(memory_root)) for path in memory_root.rglob("*") if path.is_file()
    )

    lines = [
        "# Evidence — a private fact stays private",
        "",
        "Produced by `scripts/demo/private_memory.py` against real files, with no LLM in",
        f"the loop. Docstore root for this run: `{memory_root}`.",
        "",
        f"## 1. User {_OWNER} saves a private fact",
        "",
        f'`save_fact(user_id={_OWNER}, cue="{_CUE}", content="{_CONTENT}", scope="private")`',
        "",
        "The row as `save_fact` read it back off disk:",
        "",
        "```json",
        json.dumps(saved.model_dump(mode="json"), indent=2),
        "```",
        "",
        "Every file under the docstore root after that write:",
        "",
        *[f"- `{name}`" for name in stored_files],
        "",
        "## 2. The owner recalls it on a plausible cue",
        "",
        f'`retrieve_facts(user_id={_OWNER}, query="{_QUERY}") -> {_count(owner_hits)}`',
        "",
        *[_describe(fact) for fact in owner_hits],
        "",
        f"## 3. User {_OTHER} runs the same query and gets nothing",
        "",
        f'`retrieve_facts(user_id={_OTHER}, query="{_QUERY}") -> {_count(other_hits)}`',
        "",
        f"**Not found for user {_OTHER}.** The query that matched for user {_OWNER} comes back",
        f"empty for user {_OTHER}, against the same files on the same disk.",
        "",
        "## Why",
        "",
        "`retrieve_facts` builds its read paths from the *caller's* validated id: for user",
        f"{_OTHER} it opens `private/{_OTHER}/facts.jsonl` and `shared/facts.jsonl`, and nothing",
        f"else — user {_OWNER}'s directory is not a path it can reach, so there is no filter to",
        "get wrong. `scope` is the only branch, and `private`/`shared` are the only values it has",
        "([ADR 0004](../adr/0004-three-store-memory-model.md)).",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    _EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_FILE.write_text(main(), encoding="utf-8")
    print(f"wrote {_EVIDENCE_FILE}")
