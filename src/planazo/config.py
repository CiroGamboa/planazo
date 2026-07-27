"""Shared configuration guards used by every terminal surface.

The one public helper is `check_api_key()`: an env-var lookup for
`OPENCODE_API_KEY` that prints a standard one-line message to stdout when
the key is absent and returns `False`, so each CLI/demo entrypoint can
short-circuit on a missing key with a single `if not check_api_key(): ...`
line — same wording, same "no traceback, no provider call" guarantee.

The helper is named `check_*` on purpose: `require_*` conventionally
implies raise-on-missing, and this call prints-and-returns. It never
raises, never calls the provider, and never returns the key value itself.
"""

from __future__ import annotations

import os

_MISSING_MESSAGE = (
    "OPENCODE_API_KEY is not set. Copy ../.env.example to a .env file at the "
    "repo root and set OPENCODE_API_KEY."
)


def check_api_key() -> bool:
    """Return True when `OPENCODE_API_KEY` is a non-empty string.

    Otherwise print the standard missing-key message to stdout and return
    False. Never raises, never calls the provider, never returns the key
    itself — the return value is a boolean guard, not the key.
    """
    if not os.environ.get("OPENCODE_API_KEY"):
        print(_MISSING_MESSAGE)
        return False
    return True
