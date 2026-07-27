"""The approval bounded context.

Owns the approval-gate audit trail (`ApprovalDecision`, `ApprovalRepository`)
and the runtime approval protocol (`ApprovalGate`) that `run_loop` consults
before dispatching any tool named in the gate's `tool_names`. Callers import
these three public names directly from this package.

Per [ADR 0008](../../../../../docs/adr/0008-domain-driven-module-layout.md):
this context owns approval-related persistence and the gate protocol. Rule 3
(irreversible actions require an explicit approval gate) is enforced by the
`ApprovalGate` shape defined here.
"""

from planazo.approval.gate import ApprovalGate
from planazo.approval.models import ApprovalDecision, ApprovalOutcome
from planazo.approval.repository import list_approvals, record_approval

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalOutcome",
    "list_approvals",
    "record_approval",
]
