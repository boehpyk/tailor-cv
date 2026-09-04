"""Liveness and readiness endpoints.

Two endpoints because they answer two different questions, and conflating them is how a restart loop
starts: Docker restarts a container that fails **liveness**, so if liveness also probed Postgres, a
brief database blip would kill every application container instead of letting them wait.

* `/health/live`  — is this process running? Nothing else. Docker's healthcheck uses this.
* `/health/ready` — can it serve? Probes Postgres, Redis and Celery, and returns 503 if any is down.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status

from tailorcraft.infrastructure.api.deps import EngineDep, SettingsDep
from tailorcraft.infrastructure.api.schemas.health import (
    DependencyStatus,
    LivenessResponse,
    ReadinessResponse,
)
from tailorcraft.infrastructure.health.probes import (
    probe_celery,
    probe_postgres,
    probe_redis,
)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    """Answer as long as the process can answer. It claims nothing about dependencies."""
    return LivenessResponse()


@router.get("/ready", response_model=ReadinessResponse)
async def ready(
    request: Request,
    engine: EngineDep,
    settings: SettingsDep,
    response: Response,
) -> ReadinessResponse:
    """Probe every dependency this process needs, concurrently, and report each one by name."""
    results = await asyncio.gather(
        probe_postgres(engine),
        probe_redis(settings.redis_url),
        probe_celery(request.app.state.celery),
    )

    dependencies = {r.name: DependencyStatus(healthy=r.healthy, detail=r.detail) for r in results}
    all_healthy = all(r.healthy for r in results)

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(ready=all_healthy, dependencies=dependencies)
