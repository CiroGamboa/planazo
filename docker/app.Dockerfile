# Shared Planazo application image — powers every non-source-adapter process
# (bot, scheduler, curator, agent CLI, monitor). Source adapters keep their own
# per-source image (ADR 0006); this one covers everything else so `docker
# compose up` brings the whole system up with no host-side Python or ffmpeg.
#
# Layer order mirrors docker/sources-instagram.Dockerfile so build caches stay
# hot across the two images.
#
# Design decisions live in ADR 0026 — Docker orchestration.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/src

# ffmpeg + ffprobe on $PATH — the extractor's frame path (extraction/frames.py)
# resolves both binaries from PATH and has no explicit binary override.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv installed from the official prebuilt image — no curl / apt dance.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer — cached separately from the source tree so a src/ or
# data/ edit does not invalidate the wheel install.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime-read trees outside the wheel: data/rules/, data/sources.yaml,
# data/bot.yaml, and docs/MVP-ARCHITECTURE.md are all read at runtime by
# cwd-relative or parents[N]-walked paths. compose bind-mounts ./data on
# top of /app/data so host edits are picked up on the next tick; docs/
# stays baked because the extractor reads exactly one file and it rarely
# changes at runtime.
COPY data ./data
COPY docs ./docs

# Every service picks its own command in compose.yaml — no ENTRYPOINT.
