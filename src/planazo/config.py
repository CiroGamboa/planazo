"""Environment loading, and the two env-var guards entrypoints run before work.

Importing this module loads the repository's `.env` into `os.environ`. The
file is located by anchoring to the repository root —
`Path(__file__).resolve().parents[2] / ".env"` — the same walk used by
`monitor.service.repository_root()`, `monitor.logging.default_run_log_dir()`,
`extraction.audit`, and `agents.extractor`. It is deliberately not a
working-directory search: `python -m planazo.bot` started from `/` must
resolve the same `.env` as one started from the repo root, and a discovery
walk would also let a stray `.env` above the working directory win. A missing
file is not an error — `load_dotenv` returns `False` and the ambient
environment is used unchanged, which is what CI depends on. Like the four
existing anchors, this assumes a source checkout rather than a
`site-packages` install.

Two helpers read what that leaves behind, and the `read_*` / `check_*`
contrast is deliberate:

- `check_api_key()` returns a **boolean**. `agentlib` reads `OPENCODE_API_KEY`
  out of the environment itself, so no caller needs the value — only whether
  to short-circuit.
- `read_bot_token()` returns the **value**, because `ApplicationBuilder().token(...)`
  needs the string itself.

Both print one standard line to stdout when their variable is missing, and
neither raises or calls out to a service.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _env_path() -> Path:
    """The repository root's `.env`.

    `config.py` lives at `src/planazo/config.py`, so two parents up from the
    resolved file is the repo root — one level shallower than
    `monitor/service.py`, which walks three.
    """
    return Path(__file__).resolve().parents[2] / ".env"


load_dotenv(_env_path())

_MISSING_MESSAGE = (
    "OPENCODE_API_KEY is not set. Copy ../.env.example to a .env file at the "
    "repo root and set OPENCODE_API_KEY."
)

_MISSING_TOKEN_MESSAGE = (
    "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to a .env file at the "
    "repo root and set TELEGRAM_BOT_TOKEN."
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


def read_bot_token() -> str | None:
    """Return `TELEGRAM_BOT_TOKEN`, or print the standard message and return None.

    The value comes back rather than a boolean because the caller hands it
    straight to `ApplicationBuilder().token(...)`. Unset and empty are one
    outcome: neither can start a bot, so both print and return None instead of
    letting a blank token reach the Bot API and fail there.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(_MISSING_TOKEN_MESSAGE)
        return None
    return token
