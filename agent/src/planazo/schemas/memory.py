"""Pydantic v2 models for the JSON docstore: its rows and its scope resolution.

`Fact` and `Note` are the two row shapes appended to `facts.jsonl` and
`notes.jsonl`; every append is validated through one of them before it touches
disk (AGENTS.md rule 1). `MemoryScopeRequest` is the (identity, scope) pair
resolved before any of those files is located at all.

Scope is structurally binary: `Scope` on a write, `ReadScope` on a read. There
is no `"someone else's private"` value in either type, so a write can only land
in the caller's own private directory or the one shared directory, and a read
can only ever touch those same two places.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Scope = Literal["private", "shared"]
ReadScope = Literal["private", "shared", "both"]


class MemoryScopeRequest(BaseModel):
    """The (identity, scope) pair every `memory.facts` entry point resolves first.

    `user_id` is validated as an int because it selects a *directory*:
    `Path("var/memory/private") / "1/../2"` is normalized by the filesystem to
    `var/memory/private/2` — another user's private facts. Validating the write
    path alone (`Fact.author_user_id: int`) does not close that, because the
    read path picks a directory too, so both go through this model.

    Issue #3's bot derives `user_id` from a Telegram-supplied value, which makes
    this the external-payload-into-persisted-state case AGENTS.md rule 1
    governs. `scope` is typed as `ReadScope` because this model covers reads as
    well as writes; a write's narrower `Scope` is enforced by `Fact`/`Note`.
    """

    user_id: int = Field(ge=1)
    scope: ReadScope


class Fact(BaseModel):
    """One `facts.jsonl` row — something learned about a user, cued for recall."""

    author_user_id: int
    cue: str = Field(min_length=1)
    content: str = Field(min_length=1)
    scope: Scope
    created_at: datetime


class Note(BaseModel):
    """One `notes.jsonl` row — free-form commentary attached to a single event."""

    author_user_id: int
    event_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    scope: Scope
    created_at: datetime
