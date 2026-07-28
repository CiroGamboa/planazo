"""The Telegram reply channel — `planazo.bot`'s `UserSurface` implementation.

One of the two modules in this package that import `telegram`. The command
layer receives the `UserSurface` Protocol and never learns which transport is
behind it.
"""

from __future__ import annotations

from telegram import Bot

from planazo.interfaces.surface import UserSurface


class TelegramSurface:
    """Replies to one Telegram chat through one bot.

    Replies are plain text: `send_message` is called with no `parse_mode`, so a
    preference value containing `*`, `_`, or `<` reaches the user verbatim
    rather than being reinterpreted as formatting or failing the send outright
    (ADR 0011).
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def reply(self, text: str) -> None:
        """Deliver `text` to the bound chat as plain text."""
        await self._bot.send_message(chat_id=self._chat_id, text=text)


def surface_for(bot: Bot, chat_id: int) -> UserSurface:
    """Bind `bot` and `chat_id` into the `UserSurface` a command consumes.

    The bot is passed explicitly rather than derived from the update: a
    `Message` constructed offline has no bot attached and `Message.get_bot()`
    raises, which would put a live transport in the path of every adapter test.
    The caller — `bot/app.py` — already holds `context.bot`.

    The `UserSurface` return annotation is what makes `uv run mypy src` check
    `TelegramSurface`'s conformance, here where the class is constructed.
    """
    return TelegramSurface(bot, chat_id)
