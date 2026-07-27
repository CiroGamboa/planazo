import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.memory import facts


@pytest.fixture(autouse=True)
def memory_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the docstore at a per-test tree so no test touches the repo's `var/`."""
    root = tmp_path / "memory"
    monkeypatch.setattr(facts, "MEMORY_ROOT", root)
    return root


def _rows(path: Path) -> list[dict[str, object]]:
    """Every JSON object in a JSONL file, read back off disk."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --------------------------------------------------------------------------
# Facts.
# --------------------------------------------------------------------------


def test_save_and_retrieve_fact_matches_on_cue_token_overlap() -> None:
    facts.save_fact(1, "music, subscriptions", "has a Spotify family plan", "private")

    matched = facts.retrieve_facts(1, "music events tonight")

    assert [fact.content for fact in matched] == ["has a Spotify family plan"]
    assert facts.retrieve_facts(1, "grocery shopping") == []


def test_private_fact_never_visible_to_another_user() -> None:
    facts.save_fact(1, "music", "user 1 likes techno", "private")

    assert facts.retrieve_facts(2, "music") == []
    assert [fact.content for fact in facts.retrieve_facts(1, "music")] == ["user 1 likes techno"]


def test_shared_fact_visible_to_any_user() -> None:
    facts.save_fact(1, "venues", "Razzmatazz is loud", "shared")

    assert [fact.content for fact in facts.retrieve_facts(1, "venues")] == ["Razzmatazz is loud"]
    assert [fact.content for fact in facts.retrieve_facts(2, "venues")] == ["Razzmatazz is loud"]


def test_save_fact_writes_to_the_expected_scope_directory(memory_root: Path) -> None:
    private = facts.save_fact(1, "music", "user 1 likes techno", "private")
    shared = facts.save_fact(1, "venues", "Razzmatazz is loud", "shared")

    private_file = memory_root / "private" / "1" / "facts.jsonl"
    shared_file = memory_root / "shared" / "facts.jsonl"

    assert _rows(private_file) == [private.model_dump(mode="json")]
    assert _rows(shared_file) == [shared.model_dump(mode="json")]


def test_retrieve_scope_both_unions_private_and_shared() -> None:
    facts.save_fact(1, "music", "user 1 likes techno", "private")
    facts.save_fact(1, "music", "the Sonar lineup is out", "shared")

    owner = facts.retrieve_facts(1, "music", scope="both")
    other = facts.retrieve_facts(2, "music", scope="both")

    assert {fact.content for fact in owner} == {
        "user 1 likes techno",
        "the Sonar lineup is out",
    }
    assert [fact.content for fact in other] == ["the Sonar lineup is out"]


# --------------------------------------------------------------------------
# Notes.
# --------------------------------------------------------------------------


def test_note_round_trips_and_is_scoped_by_event_id() -> None:
    facts.save_note(2, "E-123", "loud venue, arrive early", "private")

    assert [note.content for note in facts.retrieve_notes(2, "E-123")] == [
        "loud venue, arrive early"
    ]
    assert facts.retrieve_notes(2, "E-999") == []


def test_private_note_never_visible_to_another_user() -> None:
    facts.save_note(1, "E-123", "user 1 is bringing a friend", "private")

    assert facts.retrieve_notes(2, "E-123") == []
    assert [note.content for note in facts.retrieve_notes(1, "E-123")] == [
        "user 1 is bringing a friend"
    ]


# --------------------------------------------------------------------------
# The identity that picks the directory.
# --------------------------------------------------------------------------


def test_traversal_shaped_user_id_is_rejected_not_resolved(memory_root: Path) -> None:
    # Seed a real private fact for user 2 first, so the traversal has a genuine
    # target: passing here means the hole is closed, not that it looks unreachable.
    facts.save_fact(2, "music", "user 2 likes flamenco", "private")
    victim = memory_root / "private" / "2" / "facts.jsonl"
    assert _rows(victim)

    traversal = "1/../2"

    # Raising is how nothing comes back: there is no return value to leak.
    with pytest.raises(ValidationError):
        facts.retrieve_facts(traversal, "music")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        facts.save_fact(traversal, "music", "planted", "private")  # type: ignore[arg-type]

    # `MEMORY_ROOT / "private" / "1/../2"` is resolved by the filesystem to
    # `private/2`, so an unvalidated id would have appended here.
    assert [row["content"] for row in _rows(victim)] == ["user 2 likes flamenco"]
    # And no directory was created for the traversal's own first segment.
    assert sorted(entry.name for entry in (memory_root / "private").iterdir()) == ["2"]
