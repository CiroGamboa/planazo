"""Typed shapes for the HW4 Part 3 safety detector.

Kept minimal — one Pydantic model. The detector composes findings into a
list, so the outer boundary is a plain `list[SafetyFinding]`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SafetyLayer = Literal["input_filter", "output_filter", "structural", "capability"]
SafetyKind = Literal[
    "prompt_injection",
    "indirect_prompt_injection",
    "tool_abuse",
    "data_exfiltration",
    "secret_leakage",
]


class SafetyFinding(BaseModel):
    """One safety issue the detector flagged on a trace.

    `layer` — which defense layer caught it. `kind` — the attack category.
    `evidence` — the substring that tripped the rule, bounded to 200 chars
    so a long payload does not blow up the JSONL report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: SafetyLayer
    kind: SafetyKind
    evidence: str
    rationale: str
