"""Protocol interfaces at Planazo's four swap seams (ADR 0010).

Each module declares the structural contract for one axis of extensibility:

- `surface.py` — `UserSurface`: user-facing surfaces (CLI today, Telegram/WhatsApp/web later).
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
