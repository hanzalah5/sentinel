# syntax=docker/dockerfile:1

# ---- Build stage: resolve and install from the lockfile with a pinned uv -----------
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, as their own cached layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself, installed non-editable so the runtime needs no source tree.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- Runtime stage: no build tools, no uv, unprivileged user -------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8000

RUN groupadd --system --gid 10001 sentinel \
    && useradd --system --uid 10001 --gid sentinel --no-create-home sentinel

WORKDIR /app
COPY --from=build --chown=sentinel:sentinel /app/.venv /app/.venv
# Runbooks ship in the image so `python -m sentinel.rag.ingest` can run as a one-off job.
COPY --chown=sentinel:sentinel runbooks ./runbooks

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz\", timeout=2)"]

# Shell form via `sh -c` so the port follows $PORT (Cloud Run sets it); `exec` keeps
# uvicorn as PID 1 so it receives SIGTERM directly and shuts down gracefully.
CMD ["sh", "-c", "exec uvicorn sentinel.api.app:create_app --factory --host 0.0.0.0 --port \"${PORT}\""]
