"""The runtime approval-gate protocol consulted by `run_loop`.

`run_loop` routes any tool call whose `name` is in `ApprovalGate.tool_names`
through `ApprovalGate.approve(tool_name, arguments) -> bool` before dispatch.
When the approver returns True the tool runs unchanged; when it returns False
the tool is skipped, `DECLINED_RESULT` (from `planazo.agents.loop`) is emitted
as that call's `StepRecord` result, and the model receives that marker as
the call's `function_call_output`. The callback is synchronous — the loop
blocks on it, matching how `input()`-driven approvers and CI-injected fakes
actually run.

This module is Planazo's rule-3 enforcement point: irreversible tools are
never dispatched without an explicit per-call decision. The reference
`_terminal_approve` implementation lives in `agents/cli.py`; the Telegram
bot's inline-keyboard callback lands in M5 as `bot/approve.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalGate:
    """A pair of (which tools to gate, how to ask for approval).

    `tool_names` names the tools that require approval before dispatch — any
    tool call whose `name` is in this set routes through `approve` first;
    others dispatch unchanged. `approve(tool_name, arguments)` returns True
    to run the tool and False to skip it. The callback is synchronous — the
    loop blocks on it, matching how `input()` and CI-injected approvers
    actually run.
    """

    tool_names: frozenset[str]
    approve: Callable[[str, dict[str, Any]], bool]  # Any: tool args, arbitrary per tool schema
