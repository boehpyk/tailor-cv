"""Liveness and readiness endpoints.

Two endpoints because they answer two different questions, and conflating them is how a restart loop
starts: Docker restarts a container that fails **liveness**, so if liveness also probed Postgres, a
brief database blip would kill every application container instead of letting them wait.

* `/health/live`  — is this process running? Nothing else. Docker's healthcheck uses this.
* `/health/ready` — can it serve? Probes Postgres, Redis and Celery, and returns 503 if any is down.

Since slice 1.6 the readiness report also carries `jobs`, and **`jobs` is not a third thing that can
make it red** (ADR-0019). `ready` is `all(dependency.healthy)`, full stop. A guest purge that has
quietly stopped is a privacy failure on an hours-scale timeline, not an inability to serve a
request, and taking the container out of load balancing over it would fail the deploy's own
readiness gate while fixing nothing — the process that stopped is *beat*, which is not here.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status

from tailorcraft.infrastructure.api.deps import ClockDep, EngineDep, SettingsDep
from tailorcraft.infrastructure.api.schemas.health import (
    DependencyStatus,
    GuestPurgeStatus,
    JobsStatus,
    LivenessResponse,
    ReadinessResponse,
)
from tailorcraft.infrastructure.health.probes import (
    JobStatus,
    probe_celery,
    probe_guest_purge,
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
    clock: ClockDep,
    response: Response,
) -> ReadinessResponse:
    """Probe every dependency this process needs, concurrently, report each by name — and, beside
    them but never among them, report the scheduled jobs.

    **Four awaitables, two lists, and the split is load-bearing.** `results` holds `ProbeResult`s
    and only `ProbeResult`s; `guest_purge` is a `JobStatus`, which has no `healthy` attribute, so
    the `all(...)` below cannot be widened to include it by accident — it would not type-check and
    it would not run (ADR-0019 decision 2). The unpacking is written out for exactly that reason: a
    single `results = await asyncio.gather(...)` of all four would put them in one list and make the
    mistake a one-character edit away.

    All four run concurrently. The job probe costs a Redis `HGETALL` and an indexed `COUNT`, and
    serialising it behind the three dependency probes would add its latency to an endpoint the
    browser polls every 15 s and Traefik polls constantly.
    """
    postgres, redis, celery, guest_purge = await asyncio.gather(
        probe_postgres(engine),
        probe_redis(settings.redis_url),
        probe_celery(request.app.state.celery),
        probe_guest_purge(engine, settings.redis_url, settings, clock),
    )
    results = (postgres, redis, celery)

    dependencies = {r.name: DependencyStatus(healthy=r.healthy, detail=r.detail) for r in results}
    # Unchanged, and it must stay unchanged: readiness is `all(dependency.healthy)` and nothing
    # under `jobs` appears in this line (AC-32).
    all_healthy = all(r.healthy for r in results)

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        ready=all_healthy,
        dependencies=dependencies,
        jobs=JobsStatus(guest_purge=_skeleton_guest_purge_status(guest_purge)),
    )


def _guest_purge_status(job: JobStatus) -> GuestPurgeStatus:
    """Publish the probe's answer as `jobs.guest_purge`.

    **SKELETON (T27): T29 writes this body.** It is a field-for-field copy of `job` — the probe
    already owns every decision (`stale`'s truth table, the RFC 3339 rendering, the degraded
    `overdue` and its `detail`), so there is nothing for this function to compute and nothing for it
    to re-derive. That is the intended shape: a second derivation of `stale` here is exactly the
    defect slice 1.5 spent a `/verify` round on.
    """
    raise NotImplementedError("T29: copy the probe's JobStatus onto GuestPurgeStatus")


def _skeleton_guest_purge_status(job: JobStatus) -> GuestPurgeStatus:
    """The placeholder `jobs.guest_purge` this SKELETON publishes. **T29 deletes it.**

    **Why the `NotImplementedError` is not on this path, which is the one decision in T27 worth
    arguing.** `/health/ready` is a *shared* route: it already answers three dependency probes that
    three tests assert on today. A raise here would turn every readiness request into a 500, which
    fails those three existing tests for a reason that has nothing to do with them — and, worse,
    would make T28's new assertions fail on a **missing key** rather than on the assertion. An
    `ImportError` red proves a file is absent, not that an assertion discriminates (sdlc.md §2); a
    `KeyError` red proves exactly as little. So the skeleton publishes the full seven-key object
    with nothing computed in it, and T28's tests fail where they should: on `stale`, on `overdue`,
    on `last_run`.

    The argument that decides it is the same asymmetry the rest of this slice runs on: an
    over-eager skeleton costs a red that says nothing, and this one costs nothing at all, because
    `_guest_purge_status` above still carries the unimplemented signature T29 fills in.

    `job` is threaded through unused so that T29's change is one line — swap the call — rather than
    a change to the handler's shape. The probe therefore genuinely runs in the skeleton, which is
    also what proves the `asyncio.gather` above works before anybody depends on its answer.
    """
    return GuestPurgeStatus(
        scheduled=False,
        last_run=None,
        last_run_age_seconds=None,
        last_outcome=None,
        overdue=None,
        stale=False,
        detail="not implemented (T27 skeleton)",
    )
