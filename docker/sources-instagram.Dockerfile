# Docker image for the Planazo Instagram source adapter.
#
# ADR 0006 pins python:3.12-slim: instaloader is pure-Python plus a few
# C-extension deps that ship as wheels on manylinux, so slim is enough. See
# `docs/adr/0006-instagram-extraction-approach.md` for why not alpine (musl
# breaks some wheels), full-python (~1 GB, no upside), distroless (no shell
# for debug), or the Playwright base image (~1 GB unused browser).
#
# Runs one-shot: the `sources-instagram` compose service uses `restart: no`
# and the entrypoint invokes the `planazo-sources-instagram` CLI.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv installs from the official prebuilt image — no curl / apt dance.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer — cached separately from the source tree so a src/ edit
# does not invalidate the wheel install.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "--no-dev", "planazo-sources-instagram"]
