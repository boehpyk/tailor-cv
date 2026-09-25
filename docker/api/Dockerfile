# syntax=docker/dockerfile:1
#
# Multi-stage. `development` is the target the dev override builds (dev dependencies, hot reload);
# `production` is what CI pushes to GHCR and what the box pulls. Production never builds (ADR-0003).

FROM python:3.13-slim AS base

# WeasyPrint renders PDFs (ADR-0005) and needs real system libraries — Pango, Cairo, HarfBuzz and
# fontconfig. This is the least obvious dependency in the stack: without them the import succeeds
# and the *first render* fails at runtime, in the worker, where nobody is watching. Fonts matter
# too — a PDF rendered on a box with no fonts is a page of boxes.
#
# KEEP THIS LIST IN STEP WITH `.github/workflows/ci.yml`'s "WeasyPrint system libraries" step
# (search that file for this same sentence). The two drifted once already — CI was missing
# `fonts-liberation` and `libffi8` — and a missing package there fails silently too: `ruff`,
# `mypy` and every non-rendering test stay green, and only a PDF-rendering test (AC-46) notices.
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

# Uploaded and generated files live on a volume shared with the worker (ADR-0006). `state` is
# beat's schedule database (docker-compose.yml's `beat_state` volume). Both are pre-created here,
# in the image, so that when Compose mounts an EMPTY named volume over them Docker seeds the
# volume from the image directory's own ownership instead of creating the mount point as root:root.
# Without this, `state` doesn't exist in the image at all, the volume mounts as root-owned, and
# beat — which runs as `USER app` below, unlike the dev target — crash-loops on
# `PermissionError: [Errno 13] Permission denied: '.../celerybeat-schedule'`. Caught at /verify
# (slice 1.5, V1): invisible in dev because the dev override never switches off root.
RUN mkdir -p /var/lib/tailorcraft/uploads /var/lib/tailorcraft/state

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
    && chown -R app:app /app /opt/venv /var/lib/tailorcraft/uploads /var/lib/tailorcraft/state
USER app

EXPOSE 8000
# `check-settings` runs every startup refusal ONCE, in a single process, before uvicorn forks its
# workers (infrastructure/check_settings_command.py, T46/OQ-2). Without this, a refusal — a missing
# GEMINI_API_KEY or JWT_SIGNING_KEY under APP_ENV=production, a too-short JWT_SIGNING_KEY, a
# TEST_REDIS_URL colliding with a live Redis db, or a stale-window setting at or below the Celery
# hard time limit — fires at import in EVERY `--workers N` process, and uvicorn's multiprocess
# supervisor respawns the crashing import for ever: the container never becomes ready and never
# exits, so `restart: unless-stopped` never cycles it and nothing reads as a restart loop (measured
# in 1.3's T36, and again here in T46 — see CLAUDE.md). `&& exec` replaces this shell with uvicorn on
# success, so uvicorn is still PID 1 and receives signals directly; on refusal the shell exits
# non-zero and uvicorn never starts.
CMD ["sh", "-c", "python -m tailorcraft.cli check-settings && exec uvicorn tailorcraft.infrastructure.api.main:app --host 0.0.0.0 --port 8000 --workers 2"]
