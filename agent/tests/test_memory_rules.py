from pathlib import Path

import pytest

from planazo.memory import rules

# Every word of the committed seed lands in the `system` role on every run and
# competes with the tool schemas for the model's attention, which is measurable:
# the gated tool was requested 4/4 times before `search_events` existed, 2/4 once
# its schema was added, and 5/6 after an instruction-like sentence was trimmed
# from its description. These are the caps that keep that budget from drifting.
_MAX_WORDS = 120
_MAX_NON_BLANK_LINES = 10


@pytest.fixture(autouse=True)
def rules_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the rules store at a per-test directory, absent until a test creates it."""
    directory = tmp_path / "rules"
    monkeypatch.setattr(rules, "RULES_DIR", directory)
    return directory


def test_load_rules_concatenates_every_md_file_sorted(rules_dir: Path) -> None:
    rules_dir.mkdir(parents=True)
    (rules_dir / "002-b.md").write_text("SECOND", encoding="utf-8")
    (rules_dir / "001-a.md").write_text("FIRST", encoding="utf-8")
    (rules_dir / "notes.txt").write_text("NOT-MARKDOWN", encoding="utf-8")

    loaded = rules.load_rules()

    assert loaded.index("FIRST") < loaded.index("SECOND")
    assert "NOT-MARKDOWN" not in loaded


def test_load_rules_returns_empty_string_when_dir_is_absent(rules_dir: Path) -> None:
    assert not rules_dir.exists()

    assert rules.load_rules() == ""


def test_load_rules_picks_up_a_file_edit_on_the_next_call(rules_dir: Path) -> None:
    rules_dir.mkdir(parents=True)
    rule_file = rules_dir / "001-core.md"
    rule_file.write_text("RULE-A", encoding="utf-8")
    assert "RULE-A" in rules.load_rules()

    rule_file.write_text("RULE-B", encoding="utf-8")

    reloaded = rules.load_rules()
    assert "RULE-B" in reloaded
    assert "RULE-A" not in reloaded


def test_seed_rules_stay_within_the_context_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one test here that reads the real committed rules, so it overrides the
    # autouse fixture's tmp_path in-body. The directory is resolved from this
    # file rather than the cwd — `RULES_DIR`'s own default is cwd-relative, so
    # running the suite from anywhere but `agent/` would otherwise load "" and
    # let both caps pass vacuously.
    monkeypatch.setattr(rules, "RULES_DIR", Path(__file__).resolve().parents[1] / "data" / "rules")

    loaded = rules.load_rules()

    # Non-empty first: a guard that can read nothing is not a guard.
    assert loaded.strip()
    assert len(loaded.split()) <= _MAX_WORDS
    assert len([line for line in loaded.splitlines() if line.strip()]) <= _MAX_NON_BLANK_LINES
