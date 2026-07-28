"""The user-surface contract.

Swap axis: the Telegram bot (`planazo.bot.surface.TelegramSurface`) today; a
WhatsApp bot, a web frontend, a Slack app later. A surface is a **push**
channel — the transport delivers each user message into the runtime, and the
surface is what the runtime writes back through. So `UserSurface` declares
exactly one concern, and declares it as a coroutine, because every remote
transport's send is awaitable.

Approval is the module's other seam. `ApprovalCallback` is the callable a
surface hands to `planazo.approval.ApprovalGate` so an irreversible tool call
can be confirmed by the user (AGENTS.md rule 3). It is deliberately not a
`UserSurface` member: the gate consumes it directly, and a surface that gates
nothing supplies none.

Concrete implementations live per surface; the Protocols here declare the
shapes the runtime consumes, so a new surface conforms without importing an
existing one (ADR 0011).
"""

from __future__ import annotations

from typing import Any, Protocol


class ApprovalCallback(Protocol):
    """The callable an `ApprovalGate` consults before an irreversible tool call.

    Returns `True` to authorize the call, `False` to decline. The agent loop is
    synchronous and blocks here, so a surface whose confirmation arrives
    asynchronously — a Telegram inline keyboard, a web dialog — bridges at this
    boundary: run the loop off the event-loop thread with `asyncio.to_thread`,
    and hand the answer back with `asyncio.run_coroutine_threadsafe(...)`,
    blocking on the returned `concurrent.futures.Future`. Blocking *on* the
    event-loop thread instead stops the transport from ever dispatching the
    update that carries the answer, which turns the gate into a permanent
    decline that looks like it is working (ADR 0011).
    """

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> bool: ...


class UserSurface(Protocol):
    """One user-facing surface (chat bot, web, terminal) bound to the runtime.

    One member: the runtime pushes text out through it. Intake belongs to the
    transport, which dispatches messages into the runtime rather than being
    polled by it. Streaming partials, session resumption, multi-turn chat
    state, and rich-media replies land as later ADRs when a surface actually
    needs them.
    """

    async def reply(self, text: str) -> None:
        """Deliver `text` to the user."""
        ...
