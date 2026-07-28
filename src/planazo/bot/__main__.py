"""`python -m planazo.bot` — start the Telegram bot.

The exit status is `main()`'s: 0 when polling ends cleanly, 1 when
`TELEGRAM_BOT_TOKEN` is missing.
"""

from __future__ import annotations

import sys

from planazo.bot.app import main

sys.exit(main())
