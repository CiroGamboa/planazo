"""Unit tests for the Telegram reply channel.

`TelegramSurface` is driven against a recording stub bot rather than a real
`telegram.Bot`: the whole contract the command layer depends on is the outbound
call shape — one send, the bound chat, the exact text, and no `parse_mode`.
"""

from __future__ import annotations

import pytest

from planazo.bot.surface import TelegramSurface, surface_for


class _RecordingBot:
    """Records each `send_message` call's kwargs instead of reaching the Bot API."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_reply_sends_plain_text_once_to_the_bound_chat() -> None:
    bot = _RecordingBot()
    surface = TelegramSurface(bot, 4242)

    await surface.reply("city: *bold* and <b>angle</b>")

    assert len(bot.calls) == 1
    (call,) = bot.calls
    assert call["chat_id"] == 4242
    assert call["text"] == "city: *bold* and <b>angle</b>"
    assert "parse_mode" not in call


@pytest.mark.asyncio
async def test_surface_for_binds_the_given_bot_and_chat() -> None:
    bot = _RecordingBot()

    await surface_for(bot, 99).reply("hello")

    assert bot.calls == [{"chat_id": 99, "text": "hello"}]
