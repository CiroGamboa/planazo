# 0026 — Docker orchestration for the whole system

- **Status:** Accepted
- **Date:** 2026-08-06
- **Deciders:** cirogam22
- **Relates to:** [`0006-instagram-extraction-approach.md`](0006-instagram-extraction-approach.md) (per-source image isolation, extended not overturned by this ADR), [`0011-scheduled-ingestion.md`](0011-scheduled-ingestion.md) (host-cron ingestion pattern this ADR replaces), [`0020-catalog-curator-agent.md`](0020-catalog-curator-agent.md) (daily curator tick this ADR now runs in-container).

## Context

Before this ADR, only the Instagram source adapter ran in Docker (ADR 0006 landed one image + one compose service). Every other Planazo process — the Telegram bot, the `planazo-scheduler --tick` ingestion loop, the `planazo-curator --tick` daily loop, the `planazo-agent` recommender CLI, the `planazo-monitor` grading CLI — ran on the host through `uv run`. A fresh machine needed all of: Python 3.12, `uv`, `ffmpeg` + `ffprobe` on PATH, host cron wiring for the two tick loops, a populated `.env`, and a writable `var/planazo.db` before anything worked. Multiple operators reported "issues trying to run it from other computers" — that friction is exactly what a container is supposed to remove.

Goal: on any machine with Docker, `git clone`, drop in `.env`, run `docker compose up -d` — the bot polls, the scheduler ticks, the curator runs daily. No host Python, no host cron, no `uv sync`, no manual DB init.

Six shape decisions are locked here. Each names at least one rejected alternative with a reason.

**1. One shared `planazo-app` image for the whole non-adapter runtime.**
`docker/app.Dockerfile` produces a single image every non-source-adapter service uses (`bot`, `scheduler`, `curator`, `agent`, `monitor`). Layer order mirrors `docker/sources-instagram.Dockerfile` — Python 3.12-slim base, apt-installed `ffmpeg`, `uv` copied from the official prebuilt image, `pyproject.toml` + `uv.lock` + `src` + `data` + `docs` copied, `uv sync --frozen --no-dev`.

*Alternatives rejected:*

- **One image per CLI** (`docker/agent.Dockerfile`, `docker/scheduler.Dockerfile`, ...). All Planazo processes share one wheel and one dep tree; splitting adds five Dockerfiles and five build times for zero isolation benefit. ADR 0006's "per-source image" argument does not generalize — a Meta break affecting the Instagram scraper is an isolation problem the CLIs do not have.
- **One monolith image that also includes the source adapters.** Directly contradicts ADR 0006 — a Meta break would rebuild the whole image; source adapters keep their own dockerfiles.

**2. `docker compose up` starts only the always-on services; on-demand CLIs run behind profiles.**
Three services start on `up`: `bot`, `scheduler`, `curator`. Three sit behind `profiles: ["cli"]` or `profiles: ["sources"]`: `agent`, `monitor`, `sources-instagram`. They run via `docker compose run --rm <name> …` and never on `up`.

*Alternatives rejected:*

- **Everything on `up`.** The `agent` CLI is interactive (prompt + REPL), the `monitor` CLI is a diagnostic that grades recent runs, and `sources-instagram`'s `up` command is `--dry-run` (plan-print, redundant with `scheduler` doing the real ticks). Starting them on `up` is either noise or wrong.
- **No profiles, use a separate `compose.cli.yaml`.** Two compose files for a system this small is a documentation tax we do not need — profiles are the standard compose knob for exactly this.

**3. In-container `while true; sleep` loops over host cron or an in-container cron daemon.**
The scheduler and curator services run a `set -e; while true; do <cli> --tick; sleep "$INTERVAL_S"; done` shell loop. Interval defaults live in `compose.yaml` (`900` for scheduler, `86400` for curator) and are overridable via `PLANAZO_SCHEDULER_INTERVAL_S` / `PLANAZO_CURATOR_INTERVAL_S` in `.env`. An uncaught exception drops the container; `restart: unless-stopped` picks it up on the next tick — matches host-cron semantics without adding a cron daemon.

*Alternatives rejected:*

- **Host cron calling `docker compose run --rm scheduler --tick`.** Reintroduces the host-cron dependency the ADR is trying to remove. Also doubles the container-cold-start latency onto every tick (uv resolves and boots ~1s per invocation).
- **An in-container cron daemon (dcron, supercronic, cronie).** Adds a package + a syntax + a way for a silently-broken crontab to eat all future ticks. The one-line shell loop has none of those failure modes.
- **A Python-side `--daemon` flag on the CLIs.** Adds a code path (with its own tests, its own signal handling, its own restart semantics) for something a two-word shell construct already does correctly.

**4. Bind mounts over named volumes.**
`./data:/app/data:ro` (config source of truth on host, edits picked up on next tick) and `./var:/app/var` (SQLite DB + `var/*.jsonl` audit logs + `var/memory/` — the whole state footprint). No named volumes anywhere.

*Alternatives rejected:*

- **Named volumes for `var/`.** Would hide the SQLite DB inside Docker's volume tree, break the "copy the folder, hand it to a teammate" portability story, and make `sqlite3 var/planazo.db` on the host impossible.
- **A separate `data/` volume with `docker cp` for config edits.** Multiplies operator steps for a file operators already know how to edit.

**5. `PYTHONPATH=/app/src` + `data/` and `docs/` copied into the image.**
`src/planazo/config.py`, `monitor/logging.py`, `monitor/service.py`, `extraction/audit.py`, and `agents/extractor.py` all resolve runtime paths via `Path(__file__).resolve().parents[2 or 3]` — they assume a source checkout at `src/planazo/**`. When `uv sync` installs the project, the `planazo` package can be imported from `.venv/lib/.../site-packages/planazo/` and those walks then resolve to a venv path, missing `/app/data/rules/` and `/app/docs/MVP-ARCHITECTURE.md`. Setting `PYTHONPATH=/app/src` in the Dockerfile ENV forces imports to resolve from source; copying `data/` and `docs/` into the image makes those targets exist under `/app/` regardless of whether the compose mount is present.

*Alternatives rejected:*

- **Rework the `parents[N]` walks to be venv-safe.** A full-repo refactor for a Docker-only concern. The `PYTHONPATH` env fix is a one-line runtime-boundary decision.
- **Skip copying `data/` because compose bind-mounts it anyway.** Would make the image only usable inside compose. Baking a default keeps `docker run planazo-app:local` usable for ad-hoc debugging.

**6. Root user inside the container.**
The default `root` user in `python:3.12-slim` is used unchanged. No `USER planazo` line.

*Alternatives rejected:*

- **Non-root user with a pinned UID (e.g. `USER 1000`).** On Linux, bind-mounted `./var` is owned by the host user (typically UID 1000, but not always) and the container user must match, or writes fail. Making that Just Work needs `${UID}` interpolation in compose and a `useradd` in the Dockerfile — exactly the "extra setup" this ADR is trying to remove. At single-machine hobby scale the container-security benefit does not justify the operator friction. Revisit if we ever ship this image to a shared host.

## Decision

Package the whole non-adapter Planazo runtime in one `planazo-app` image built by `docker/app.Dockerfile`, orchestrate it with `compose.yaml`, and make `docker compose up -d` the canonical way to run the system. Long-running processes (`bot`, `scheduler`, `curator`) start on `up`; on-demand CLIs (`agent`, `monitor`, `sources-instagram`) sit behind compose profiles and run via `docker compose run --rm …`. Scheduler and curator use in-container shell sleep loops; bind mounts back `./data` (ro) and `./var` (rw); `PYTHONPATH=/app/src` keeps source-checkout path walks intact; the image runs as root; `sources-instagram` keeps its own image per ADR 0006.

## Consequences

### Positive

- **One-command onboarding.** `git clone` + `.env` + `docker compose up -d` is the entire setup story. No Python, no uv, no ffmpeg, no host cron.
- **Portability guarantee.** `.env` + `data/` + `var/` copied to another machine = a working install. The state footprint is knowable and reviewable.
- **ADR 0006's isolation stays intact.** Source adapters still have their own images; a Meta break rebuilds only `planazo-sources-instagram`.
- **Cron friction removed.** README-package.md's "wire the tick into cron every 15 minutes" section becomes "start the `scheduler` service."
- **Fail-loud semantics match host cron.** An unhandled exception drops the container, logs the message, and `restart: unless-stopped` retries on the next tick.
- **Native run still works.** `uv run planazo-agent`, `uv run pytest`, `uv run ruff check` on the host are unchanged — Docker is additive.

### Negative / accepted trade-offs

- **Runs as root inside the container.** See decision 6. Acceptable at single-machine hobby scale; revisit for shared-host deployments.
- **The image bakes `data/` and `docs/`.** Rebuilding on every config or doc edit is not required (the bind mount overrides at runtime), but a `docker compose build` skips build cache for those layers when they change. Fine at the current edit cadence.
- **`ffmpeg` adds ~30 MB to the image.** Every service that includes the extractor path needs it, and the plan explicitly rejects splitting into an "extract" variant.
- **`while true; sleep` cannot skew tick times.** Sleeps drift by tick duration; over a day the scheduler runs slightly fewer ticks than a cron-scheduled equivalent. Acceptable — the cadence gate in `sources.yaml` is the actual cadence source of truth.

### Follow-ups

- Update `AGENTS.md` "Setup & commands" to lead with `docker compose up -d`.
- Update `README-package.md` to add a "Docker" quick-start block and replace the host-cron section.
- Add `PLANAZO_SCHEDULER_INTERVAL_S` and `PLANAZO_CURATOR_INTERVAL_S` to `.env.example` as optional overrides.
- If a future deployment target needs a non-root container (shared host, security-review requirement), reopen decision 6 as a superseding ADR.
- If ingestion outgrows a single-machine deployment, this ADR is the one to supersede — named volumes, image versioning, and a registry push all become relevant then.
