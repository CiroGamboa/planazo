"""Typed shapes for the HW4 Part 3 safety detector.

Kept minimal — one Pydantic model. The detector composes findings into a
list, so the outer boundary is a plain `list[SafetyFinding]`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SafetyLayer = Literal["input_filter", "output_filter", "structural", "capability"]
SafetyKind = Literal[
    "prompt_injection",
    "indirect_prompt_injection",
    "tool_abuse",
    "data_exfiltration",
    "secret_leakage",
]

_MAX_EVIDENCE_CHARS = 200
_MAX_RATIONALE_CHARS = 500


class SafetyFinding(BaseModel):
    """One safety issue the detector flagged on a trace.

    `layer` — which defense layer caught it. `kind` — the attack category.
    `evidence` — the substring that tripped the rule, capped at 200 chars
    by the Pydantic constraint so a long payload cannot blow up the
    JSONL report even if the detector's own truncation ever regresses.
    `rationale` — a short human-readable reason bounded to 500 chars.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: SafetyLayer
    kind: SafetyKind
    evidence: str = Field(max_length=_MAX_EVIDENCE_CHARS)
    rationale: str = Field(max_length=_MAX_RATIONALE_CHARS)
