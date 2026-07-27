"""The user-surface contract.

Swap axis: the terminal `planazo-agent` CLI today; a Telegram bot (M5), a
WhatsApp bot, a web frontend, a Slack app later. Every surface exposes
three concerns to the agent runtime:

- **Receive** a user message.
- **Reply** with the runtime's final answer (or a streaming partial).
- **Ask for approval** for irreversible tool calls (an `ApprovalGate`).

Concrete implementations live per surface: `planazo.agents.cli`'s
`_terminal_approve` today; a future `planazo.bot.approve` for Telegram.
The Protocol here declares the shape the runtime consumes; a WhatsApp
surface conforms without importing the terminal implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class ApprovalCallback(Protocol):
    """The callable each surface supplies to an `ApprovalGate`.

    Returns `True` to authorize the tool call, `False` to decline. The
    runtime blocks on this call — surfaces that need async behaviour
    (Telegram inline keyboards, web UI confirmations) bridge to sync at
    this boundary.
    """

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> bool: ...


class UserSurface(Protocol):
    """One user-facing surface (terminal, chat bot, web) bound to the runtime.

    Minimal shape today — receive one message, reply once, optionally
    supply an approval callback for gated tools. Streaming partials, session
    resumption, multi-turn chat state, and rich-media replies land as
    later ADRs when a surface actually needs them.
    """

    def read_message(self) -> str:
        """Return the user's next input; blocks until one arrives."""
        ...

    def reply(self, text: str) -> None:
        """Deliver `text` to the user."""
        ...

    def approval_callback(self) -> Callable[[str, dict[str, Any]], bool]:
        """Return the callback the runtime's `ApprovalGate` will consult."""
        ...
