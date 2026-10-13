# syntax=docker/dockerfile:1

# --- base: Python, uv and the locked runtime dependencies ---------------------------------
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /bin/uv

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY samples ./samples
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run as an unprivileged user; the uploads volume inherits this ownership.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data
ENV UPLOAD_DIR=/data/uploads

# --- test: adds dev dependencies and the test suite ---------------------------------------
FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY tests ./tests
COPY scripts ./scripts
USER app
CMD ["pytest"]

# --- runtime: what api, worker and migrate run --------------------------------------------
FROM base AS runtime
USER app
EXPOSE 8000
CMD ["uvicorn", "chart_analyzer.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
