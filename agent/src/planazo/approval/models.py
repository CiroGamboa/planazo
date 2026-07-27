"""Pydantic v2 row model for the `approvals` table.

Every field matches its column in `planazo/storage/schema_v1.sql` 1:1, so a
row is validated on the way in (AGENTS.md rule 1) and reconstructed on the
way out. `id` and `decided_at` are `None` until the row exists: a caller
builds an `ApprovalDecision` to insert without knowing its id, and the
repository stamps `decided_at` when the row is written.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ApprovalOutcome = Literal["approve", "reject"]


class ApprovalDecision(BaseModel):
    """One `approvals` row — the audit trail for an approval-gate decision."""

    id: int | None = None
    user_id: int = Field(ge=1)
    artifact_kind: str = Field(min_length=1)
    artifact_id: int
    decision: ApprovalOutcome
    decided_at: datetime | None = None
