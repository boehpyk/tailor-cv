# syntax=docker/dockerfile:1
#
# Multi-stage. `development` is the target the dev override builds (dev dependencies, hot reload);
# `production` is what CI pushes to GHCR and what the box pulls. Production never builds (ADR-0003).

FROM python:3.13-slim AS base

# WeasyPrint renders PDFs (ADR-0005) and needs real system libraries — Pango, Cairo, HarfBuzz and
# fontconfig. This is the least obvious dependency in the stack: without them the import succeeds
# and the *first render* fails at runtime, in the worker, where nobody is watching. Fonts matter
# too — a PDF rendered on a box with no fonts is a page of boxes. Mirrored in .github/workflows/ci.yml;
# keep the two lists in step.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
        libffi8 shared-mime-info fonts-dejavu-core fonts-liberation \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # The virtualenv lives OUTSIDE /app on purpose. The dev override bind-mounts ./api over /app, so
    # a venv at the uv default of /app/.venv would be shadowed by whatever the host has there — and
    # a host venv points at a host interpreter path that does not exist in the container. The
    # symptom is an exec format / missing-interpreter error that looks like a broken image rather
    # than a mount collision.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    # The SAFE value is the default in the image. Dev-ness comes from the compose override, never
    # from a value someone remembered to change in a gitignored .env (infrastructure.md).
    APP_ENV=production \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Uploaded and generated files live on a volume shared with the worker (ADR-0006).
RUN mkdir -p /var/lib/tailorcraft/uploads

# ---------------------------------------------------------------------------
FROM base AS deps
# Dependency layer, cached on the lockfile alone so a source edit does not reinstall the world.
# `--no-install-project` because the project's own source is not here yet — installing it would need
# src/, which arrives in the stages below.
COPY api/pyproject.toml api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
FROM base AS development
COPY api/pyproject.toml api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project
COPY api/ /app/
# Now that src/ exists, install the project itself — editable, so a bind-mounted edit is picked up
# without a rebuild.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
CMD ["uvicorn", "tailorcraft.infrastructure.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
FROM base AS production
COPY --from=deps /opt/venv /opt/venv
COPY api/ /app/
# `--no-editable` so the package is copied into the venv rather than linked to /app: the running
# container then does not depend on the source tree still being where the build left it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Run as a non-root user. This process writes uploaded CVs — files whose names come from strangers —
# into a mounted volume. Root is not the account that should be doing that.
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app /opt/venv /var/lib/tailorcraft/uploads
USER app

EXPOSE 8000
CMD ["uvicorn", "tailorcraft.infrastructure.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
