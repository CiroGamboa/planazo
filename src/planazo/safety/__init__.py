"""HW4 Part 3 safety-hardening layer for Planazo.

Two live defense layers, two cited ones (ADR 0027 decision 6):

- `detect.detect_input_injection(text)` — Layer 1: input filtering. Flags a
  handful of well-known prompt-injection surface patterns before the model
  ever sees the message.
- `detect.detect_output_leakage(answer, context)` — Layer 3: output
  filtering. Flags an answer that leaks API-key-shaped tokens or private
  memory content into a user-facing surface.
- Layer 2 (structural separation between untrusted retrieved text and the
  system role) is enforced by AGENTS.md rule 2 + `event_agent.run_once`'s
  push-context assembly (ADR 0004). This module cites it, does not
  re-implement it.
- Layer 4 (capability constraints — the memory-tool user-id closure,
  approval-gate on irreversible tools) is enforced by ADR 0002 + ADR 0004.
  Same story: cited, not re-implemented.

`detect.detect_safety_issues(trace)` composes the two live layers into a
pure function of an MLflow trace so the same code runs against stored
traces (batch mode) and inline (live).
"""

from planazo.safety.detect import (
    detect_input_injection,
    detect_output_leakage,
    detect_safety_issues,
)
from planazo.safety.models import SafetyFinding

__all__ = [
    "SafetyFinding",
    "detect_input_injection",
    "detect_output_leakage",
    "detect_safety_issues",
]
