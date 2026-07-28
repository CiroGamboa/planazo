"""Contract tests for `planazo.bot.models.IncomingMessage`.

The model is the bot's trust boundary (AGENTS.md rule 1), so what is locked
here is what it refuses: an unnamed sender, an empty body, and any field the
adapter was not supposed to carry across.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from planazo.bot.models import IncomingMessage


def test_empty_display_name_rejects() -> None:
    with pytest.raises(ValidationError):
        IncomingMessage(telegram_user_id="4242", display_name="", text="/start")


def test_empty_text_rejects() -> None:
    with pytest.raises(ValidationError):
        IncomingMessage(telegram_user_id="4242", display_name="Dani V", text="")


def test_extra_field_rejects() -> None:
    """`chat_id` belongs to the surface, not to the message the commands read."""
    with pytest.raises(ValidationError):
        IncomingMessage.model_validate(
            {
                "telegram_user_id": "4242",
                "display_name": "Dani V",
                "text": "/start",
                "chat_id": 99,
            }
        )


def test_absent_telegram_handle_accepts() -> None:
    message = IncomingMessage(telegram_user_id="4242", display_name="Dani V", text="/start")

    assert message.telegram_handle is None
