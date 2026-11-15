# syntax=docker/dockerfile:1

# ---- Builder: resolve runtime dependencies into a project-local venv ----
# The uv base image ships uv + CPython 3.12, so no separate install step.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Compile bytecode for faster cold start; copy (not hardlink) so the venv
# is self-contained when moved to the runtime stage; pin the venv path.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Only the manifests first: this layer stays cached until deps change.
COPY pyproject.toml uv.lock ./

# Runtime deps only (no dev group). package=false → no project install here.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---- Runtime: slim image, non-root, just the venv + application code ----
FROM python:3.15-slim-bookworm AS runtime

# Dedicated unprivileged user/group; the process never runs as root
# (SECURITY.md — Container control).
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

# Put the pre-built virtualenv first on PATH so `uvicorn`/`alembic` resolve
# to it directly (no `uv run` needed inside the container).
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Application source. In dev, docker-compose bind-mounts over this so
# --reload picks up host edits; in prod the baked copy is what runs.
COPY --chown=app:app app ./app

# Pre-download the embedding model and the tiktoken vocabulary so the container
# starts offline/reproducibly and never writes a cache at runtime
# (bge-small-en-v1.5 ≈ 130 MB; cl100k_base ≈ 1.6 MB). Both env vars pin the
# cache location — set here, they take precedence over app.paths' defaults, so
# nothing is ever written outside /app.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed_cache \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache
RUN mkdir -p /app/.tiktoken_cache \
    && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chown -R app:app /app/.fastembed_cache /app/.tiktoken_cache

# Alembic config + migration scripts. Baked in (not bind-mounted) so
# `docker compose exec api alembic upgrade head` works in dev and prod alike.
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic

# Admin CLI (`make apikey`). Baked in (not bind-mounted) so it is available in
# dev and prod for `docker compose exec api python scripts/create_api_key.py`.
COPY --chown=app:app scripts ./scripts

USER app

EXPOSE 8000

# Default (prod-style) command; the dev compose overrides it with --reload.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
