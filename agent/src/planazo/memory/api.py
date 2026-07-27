"""The LLM-facing memory tools, bound to one identity per run.

`build_memory_tools(user_id)` returns one run's tool schemas plus the registry
`run_loop` dispatches through. The four callables in it — `retrieve_memory`,
`save_memory`, `retrieve_notes`, `save_note` — are nested closures over the
validated `user_id`: it is a captured free variable, so it appears in no
signature, `schema_for` cannot see it, and no tool-call argument can supply it.
A tool call that carries a `user_id` key anyway raises `TypeError`, which
`run_loop`'s dispatch turns into a `tool_failed` marker — a hard failure rather
than a silent scope override. Binding the identity with a `partial` instead is
not equivalent: a partial's bound keyword *is* overridable by a caller passing
the same key again, which is the one hole this shape closes.

`user_id` is validated once at build time, before any LLM call, so a bad
identity fails while composing the run instead of mid-loop.

Each wrapper mirrors `tools.tools`'s two-layer error split: the `Literal` scope
annotations become a JSON-schema `enum` the provider enforces, and the wrapper
*also* catches `ValidationError` from the `memory.facts` call and returns a
typed `invalid_memory_data` (writes) or `invalid_memory_query` (reads) state, so
a model that emits `scope="global"` or an empty `cue` gets a branch it can
correct on its next turn (AGENTS.md rule 4). Results are serialized with
`model_dump(mode="json")` because `run_loop` feeds tool output through
`json.dumps`, which cannot serialize a `BaseModel`.

The private/shared guarantee this module rests on is
`docs/adr/0004-three-store-memory-model.md`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from planazo.memory import facts
from planazo.schemas.memory import MemoryScopeRequest, ReadScope, Scope
from tools.schema import schema_for


def build_memory_tools(
    user_id: int,
    # Any: JSON Schema documents, as produced by schema_for.
) -> tuple[list[dict[str, Any]], dict[str, Callable[..., dict[str, object]]]]:
    """Build one run's memory tool schemas and registry, bound to `user_id`.

    Raises `ValidationError` when `user_id` is not a positive integer — a
    traversal-shaped value like `"1/../2"` never reaches a directory, and the
    failure lands at composition time rather than on the model's first call.
    """
    owner = MemoryScopeRequest(user_id=user_id, scope="both").user_id

    def retrieve_memory(query: str, scope: ReadScope = "both") -> dict[str, object]:
        """Recall previously saved facts about the user that relate to `query`.

        Call this to check what is already known before asking the user to
        repeat themselves, for example on "find me something I'd like": the
        `query` is matched against each fact's cue by word overlap, so pass the
        words you would expect the fact to be filed under. `scope` selects
        which store to read: `"private"` for this user's own facts, `"shared"`
        for facts anyone may read, `"both"` for the union. An empty `facts`
        list means nothing on file matches, not that the lookup failed.
        """
        try:
            found = facts.retrieve_facts(owner, query, scope)
        except ValidationError as exc:
            return {"error_type": "invalid_memory_query", "message": str(exc)}
        return {
            "facts": [fact.model_dump(mode="json") for fact in found],
            "total": len(found),
        }

    def save_memory(cue: str, content: str, scope: Scope) -> dict[str, object]:
        """Remember one durable fact about the user for later runs.

        Call this when the user states something that stays true beyond this
        conversation — a taste, a constraint, a habit. `cue` is the handful of
        words the fact should later be recalled by, `content` is the fact
        itself, and `scope` is `"private"` for this user only or `"shared"` for
        something any user may read. Do NOT save a one-off request or anything
        the user has not actually said. `total_facts` reports how many facts
        this user can now recall on that cue.
        """
        try:
            saved = facts.save_fact(owner, cue, content, scope)
        except ValidationError as exc:
            return {"error_type": "invalid_memory_data", "message": str(exc)}
        return {
            "saved": saved.model_dump(mode="json"),
            "total_facts": len(facts.retrieve_facts(owner, cue, "both")),
        }

    def retrieve_notes(event_id: str, scope: ReadScope = "both") -> dict[str, object]:
        """Read the notes filed against one specific event.

        Call this with an `event_id` when the user asks what is known or what
        people have said about that event. `scope` selects which store to read:
        `"private"` for this user's own notes, `"shared"` for notes anyone may
        read, `"both"` for the union. An empty `notes` list means no note is
        filed against that event.
        """
        try:
            found = facts.retrieve_notes(owner, event_id, scope)
        except ValidationError as exc:
            return {"error_type": "invalid_memory_query", "message": str(exc)}
        return {
            "notes": [note.model_dump(mode="json") for note in found],
            "total": len(found),
        }

    def save_note(event_id: str, content: str, scope: Scope) -> dict[str, object]:
        """File one free-form note against a specific event.

        Call this when the user says something about one event in particular —
        "that venue is loud", "I went last year" — passing the `event_id` it
        belongs to. `scope` is `"private"` for this user only or `"shared"` for
        a note any user may read. Do NOT use this for facts about the user in
        general; those go to `save_memory`. `total_notes` reports how many notes
        this user can now read on that event.
        """
        try:
            saved = facts.save_note(owner, event_id, content, scope)
        except ValidationError as exc:
            return {"error_type": "invalid_memory_data", "message": str(exc)}
        return {
            "saved": saved.model_dump(mode="json"),
            "total_notes": len(facts.retrieve_notes(owner, event_id, "both")),
        }

    registry: dict[str, Callable[..., dict[str, object]]] = {
        "retrieve_memory": retrieve_memory,
        "save_memory": save_memory,
        "retrieve_notes": retrieve_notes,
        "save_note": save_note,
    }
    return [schema_for(tool) for tool in registry.values()], registry
