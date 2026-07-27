import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.memory import api, facts

# One full, valid argument set per tool name. Reused by the injected-`user_id`
# test so every closure is called exactly as the model would call it — with
# nothing missing — which is what makes the resulting TypeError attributable to
# `user_id` and nothing else.
_VALID_CALLS: dict[str, dict[str, object]] = {
    "retrieve_memory": {"query": "music", "scope": "both"},
    "save_memory": {"cue": "music", "content": "likes techno", "scope": "private"},
    "retrieve_notes": {"event_id": "E-123", "scope": "both"},
    "save_note": {"event_id": "E-123", "content": "loud venue", "scope": "private"},
}


@pytest.fixture(autouse=True)
def memory_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the docstore at a per-test tree so no test touches the repo's `var/`."""
    root = tmp_path / "memory"
    monkeypatch.setattr(facts, "MEMORY_ROOT", root)
    return root


# --------------------------------------------------------------------------
# The identity the model cannot reach.
# --------------------------------------------------------------------------


def test_no_memory_tool_schema_exposes_user_id() -> None:
    schemas, _ = api.build_memory_tools(1)

    assert {schema["name"] for schema in schemas} == {
        "retrieve_memory",
        "save_memory",
        "retrieve_notes",
        "save_note",
    }
    for schema in schemas:
        assert "user_id" not in schema["parameters"]["properties"]
        assert schema["parameters"]["additionalProperties"] is False


def test_injected_user_id_is_rejected_by_every_memory_closure() -> None:
    _, registry = api.build_memory_tools(1)

    for name, arguments in _VALID_CALLS.items():
        with pytest.raises(TypeError) as excinfo:
            registry[name](**arguments, user_id=3)
        # Naming the parameter matters: a TypeError for a *missing* argument
        # would also be raised if the closure happily accepted `user_id`.
        assert "user_id" in str(excinfo.value)

        # Positive control — the identical call without `user_id` succeeds, so
        # the rejection above is about `user_id` and not about the call shape.
        assert "error_type" not in registry[name](**arguments)


def test_a_private_fact_is_reachable_only_through_its_owners_closure() -> None:
    _, owner = api.build_memory_tools(1)
    _, other = api.build_memory_tools(2)

    owner["save_memory"](cue="music", content="user 1 likes techno", scope="private")

    assert other["retrieve_memory"](query="music") == {"facts": [], "total": 0}
    found = owner["retrieve_memory"](query="music")
    assert [fact["content"] for fact in found["facts"]] == ["user 1 likes techno"]
    assert found["total"] == 1


def test_build_memory_tools_rejects_an_unusable_user_id() -> None:
    # `MEMORY_ROOT / "private" / "1/../2"` resolves to user 2's directory, so a
    # traversal-shaped id has to fail while the run is being composed.
    with pytest.raises(ValidationError):
        api.build_memory_tools("1/../2")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        api.build_memory_tools(0)


# --------------------------------------------------------------------------
# Success shapes.
# --------------------------------------------------------------------------


def test_every_success_return_survives_json_dumps() -> None:
    # run_loop feeds tool output through json.dumps to build the
    # function_call_output message, so a returned model instance would break
    # the run rather than the tool.
    _, registry = api.build_memory_tools(1)

    for name, arguments in _VALID_CALLS.items():
        result = registry[name](**arguments)
        assert json.loads(json.dumps(result)) == result


def test_saving_then_retrieving_reports_the_same_facts() -> None:
    _, registry = api.build_memory_tools(1)

    saved = registry["save_memory"](cue="music", content="likes techno", scope="private")

    assert saved["saved"]["content"] == "likes techno"
    assert saved["saved"]["author_user_id"] == 1
    assert saved["total_facts"] == registry["retrieve_memory"](query="music")["total"]


def test_saving_then_retrieving_reports_the_same_notes() -> None:
    _, registry = api.build_memory_tools(1)

    saved = registry["save_note"](event_id="E-123", content="loud venue", scope="shared")

    assert saved["saved"]["event_id"] == "E-123"
    assert saved["total_notes"] == registry["retrieve_notes"](event_id="E-123")["total"]


# --------------------------------------------------------------------------
# Typed error branches — nothing raises, nothing lands on disk.
# --------------------------------------------------------------------------


def test_saving_a_fact_with_an_empty_cue_is_a_typed_error(memory_root: Path) -> None:
    _, registry = api.build_memory_tools(1)

    result = registry["save_memory"](cue="", content="x", scope="private")

    assert result["error_type"] == "invalid_memory_data"
    assert not memory_root.exists()


def test_saving_a_fact_with_an_unknown_scope_is_a_typed_error(memory_root: Path) -> None:
    _, registry = api.build_memory_tools(1)

    result = registry["save_memory"](cue="c", content="x", scope="global")

    assert result["error_type"] == "invalid_memory_data"
    assert not memory_root.exists()


def test_saving_a_fact_with_the_read_only_both_scope_is_a_typed_error(memory_root: Path) -> None:
    # "both" is the one scope value a write can carry that MemoryScopeRequest
    # accepts (it is typed ReadScope), so only `Fact.scope` catches it — and only
    # because the Fact is built before the append. An inverted ordering would
    # land this write in the shared file, readable by everyone.
    _, registry = api.build_memory_tools(1)

    result = registry["save_memory"](cue="c", content="x", scope="both")

    assert result["error_type"] == "invalid_memory_data"
    assert not (memory_root / "shared" / "facts.jsonl").exists()
    assert not memory_root.exists()


def test_saving_a_note_with_an_empty_event_id_is_a_typed_error(memory_root: Path) -> None:
    _, registry = api.build_memory_tools(1)

    result = registry["save_note"](event_id="", content="x", scope="shared")

    assert result["error_type"] == "invalid_memory_data"
    assert not memory_root.exists()


def test_retrieving_with_an_unknown_scope_is_a_typed_error() -> None:
    _, registry = api.build_memory_tools(1)

    result = registry["retrieve_memory"](query="x", scope="global")

    assert result["error_type"] == "invalid_memory_query"
    assert "facts" not in result


def test_retrieving_notes_with_an_unknown_scope_is_a_typed_error() -> None:
    _, registry = api.build_memory_tools(1)

    result = registry["retrieve_notes"](event_id="E-123", scope="global")

    assert result["error_type"] == "invalid_memory_query"
    assert "notes" not in result
