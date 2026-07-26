# Planazo

An agentic Barcelona event-discovery assistant. A user asks for events matching a time window and interests; Planazo gathers candidates from selected sources, extracts and validates the details, ranks them, and — on explicit approval — creates a Google Calendar entry.

## Quick start

```bash
uv sync
uv run pytest
# App entry command goes here once it exists, e.g.:
# uv run python -m planazo.bot
```

## Working on the project

- **Rulebook:** [`AGENTS.md`](AGENTS.md) — read this first.
- **Product spec:** [`docs/PLANAZO-PROJECT-CONTEXT.md`](docs/PLANAZO-PROJECT-CONTEXT.md).
- **Decisions:** [`docs/adr/`](docs/adr/) — numbered architecture decision records.
- **Tickets:** GitHub Issues. Use `/writing-development-tickets` in Claude Code to scope one, `/executing-development-tickets` to drive it end-to-end.
