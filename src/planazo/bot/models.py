"""The bot's trust boundary — one validated inbound message.

A Telegram update is an external payload, so it crosses into Planazo through a
Pydantic v2 model before any command reads it (AGENTS.md rule 1). This module
imports nothing from `telegram`: `bot/app.py` owns the conversion, which is
what keeps `commands.py` and `session.py` transport-neutral and offline
testable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IncomingMessage(BaseModel):
    """One user message, projected out of a transport update.

    Frozen, because the command layer reads it and never rewrites it.
    `extra="forbid"` so a field an adapter invents — or one the Bot API renames
    upstream — fails loudly at the boundary instead of travelling on as
    unvalidated state.

    The reply channel is deliberately absent. It is bound inside the
    `UserSurface` handed to the command alongside this message, so no chat
    identifier travels through the command layer at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    telegram_user_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    telegram_handle: str | None = None
    text: str = Field(min_length=1)
