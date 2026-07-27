# Planazo

An agentic Barcelona event-discovery assistant. A user asks for events matching a time window and interests; Planazo gathers candidates from selected sources, extracts and validates the details, ranks them, and — on explicit approval — creates a Google Calendar entry.

## Quick start

The agent runtime lives under [`src/planazo/`](src/planazo/):

```bash
uv sync
uv run pytest
uv run planazo-agent --calendar "save a tech event evt-1 called AI Meetup at 2026-08-01T19:00:00 in Barcelona, confidence 0.9"
# uv run python -m planazo.bot   # Telegram bot entry command goes here once it exists
```

See [`README-package.md`](README-package.md) for the full CLI, the tools, and the approval-gate behavior.

## Working on the project

- **Rulebook:** [`AGENTS.md`](AGENTS.md) — read this first.
- **Product spec:** [`docs/PLANAZO-PROJECT-CONTEXT.md`](docs/PLANAZO-PROJECT-CONTEXT.md).
- **Decisions:** [`docs/adr/`](docs/adr/) — numbered architecture decision records.
- **Tickets:** GitHub Issues. Use `/writing-development-tickets` in Claude Code to scope one, `/executing-development-tickets` to drive it end-to-end.
