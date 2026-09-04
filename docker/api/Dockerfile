# syntax=docker/dockerfile:1
#
# Multi-stage. `development` is the target the dev override builds (dev dependencies, hot reload);
# `production` is what CI pushes to GHCR and what the box pulls. Production never builds (ADR-0003).

FROM python:3.13-slim AS base

# WeasyPrint renders PDFs (ADR-0005) and needs real system libraries — Pango, Cairo, HarfBuzz and
# fontconfig. This is the single least obvious dependency in the stack: without these the import
# succeeds and the *first render* fails at runtime, in the worker, where nobody is watching.
# Fonts matter too: a PDF rendered on a box with no fonts is a page of boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
        libffi8 shared-mime-info fonts-dejavu-core fonts-liberation \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # The SAFE value is the default in the image. Dev-ness comes from the compose override, never
    # from a value someone remembered to change in a gitignored .env (infrastructure.md).
    APP_ENV=production \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# The uploads directory is a volume in every environment, shared with the worker (ADR-0006).
RUN mkdir -p /var/lib/tailorcraft/uploads

# ---------------------------------------------------------------------------
FROM base AS deps
# Dependency layer, cached on the lockfile alone so a source edit does not reinstall the world.
COPY api/pyproject.toml api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
FROM base AS development
COPY api/pyproject.toml api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
COPY api/ /app/
CMD ["uvicorn", "tailorcraft.infrastructure.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
FROM base AS production
COPY --from=deps /app/.venv /app/.venv
COPY api/ /app/

# Run as a non-root user. The api writes uploaded CVs — files whose names come from strangers — into
# a mounted volume; root is not the account that should be doing that.
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app /var/lib/tailorcraft/uploads
USER app

EXPOSE 8000
CMD ["uvicorn", "tailorcraft.infrastructure.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
