"""The Telegram bot bounded context — Planazo's user-facing surface.

Six modules with one dependency direction, so that only the package's edge
knows what Telegram is:

- `app.py` — builds the `Application`, registers one `CommandHandler` per
  command, converts each `Update` into an `IncomingMessage`, and runs long
  polling. Imports `telegram`.
- `surface.py` — `TelegramSurface`, the reply channel bound to a `Bot` and a
  `chat_id`; the implementation of `planazo.interfaces.surface.UserSurface`.
  Imports `telegram`.
- `models.py` — `IncomingMessage`, the validated projection of one update.
- `session.py` — `resolve_user`, Telegram id to internal `users` row.
- `commands.py` — the command coroutines and every user-facing literal.
- `config.py` — `BotConfig`, the Pydantic v2 schema for `data/bot.yaml` (the
  message catalog, locales, and registration-step declarations), its
  `load_config()` loader, and `resolve()`, the locale-aware message lookup.

The last four import no transport, which is what makes every command
exercisable offline against real SQLite and a recording surface. Each command
has the same signature — `(surface: UserSurface, conn: sqlite3.Connection,
message: IncomingMessage) -> None` — so the surface behind it is swappable.
`__main__.py` is the entry shim behind `python -m planazo.bot`, and holds
nothing but `sys.exit(main())`.

**No LLM call originates here** (ADR 0011). The commands are CRUD against
SQLite; no module under `bot/` names the LLM wrapper package, and
`tests/test_bot_no_llm.py` is the guard that reads this package's source text
to prove it.
"""
