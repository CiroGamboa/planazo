"""Protocol interfaces at Planazo's four swap seams (ADR 0010).

Each module declares the structural contract for one axis of extensibility:

- `surface.py` — `UserSurface` (shape per ADR 0011): user-facing surfaces.
  `planazo.bot.surface.TelegramSurface` implements it; WhatsApp and web
  surfaces conform the same way, and #60 gives the terminal CLI its own.
  `ApprovalCallback` lives here too — the callable a surface hands to an
  `ApprovalGate`.
- `sources.py` — `EventSource`: data-source adapters (Instagram, TikTok, YouTube,
  news, Meetup, Eventbrite).
- `persistence.py` — `Repository[T]`: per-aggregate persistence (SQLite today,
  Postgres later).
- `runtime.py` — `AgentLoop`: the agent runtime kernel (`run_loop` today,
  LangChain / LangGraph later).

Every Protocol here uses Python's structural typing — no `abc.ABC` inheritance
required. A concrete class satisfies a Protocol by having the right shape;
callers accept the Protocol type, callers construct the concrete type.

Concrete implementations live in their owning bounded contexts. This package
holds only the shapes.
"""
