"""Readiness probes.

`/health/ready` answers one question: *can this process do its job right now?* Answering it honestly
means probing the things that can be down, and this stack has three.

The third one is the point. Postgres and Redis are the obvious two, and probing only those was
enough to let the previous project run for an entire session with a crash-looping background worker
behind a green dashboard. **A stopped worker looks exactly like a healthy system** if nothing asks
it a question: the API answers, the database answers, and every export queues silently forever
(ADR-0005). So Celery is probed too.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.redis_client import create_redis

log = structlog.get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of probing one dependency."""

    name: str
    healthy: bool
    detail: str | None = None


async def probe_postgres(engine: AsyncEngine) -> ProbeResult:
    """Round-trip a query. Never `return True` — an unused connection proves nothing."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("probe.postgres.failed", error=type(exc).__name__)
        return ProbeResult("postgres", healthy=False, detail=type(exc).__name__)
    return ProbeResult("postgres", healthy=True)


async def probe_redis(redis_url: str) -> ProbeResult:
    """PING Redis.

    Redis is load-bearing for a user-visible feature here, not merely a cache: it is the Celery
    broker and result backend, so Redis down is "no exports and no purges", not "slower".
    """
    client = create_redis(redis_url)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await client.ping()
    except Exception as exc:
        log.warning("probe.redis.failed", error=type(exc).__name__)
        return ProbeResult("redis", healthy=False, detail=type(exc).__name__)
    finally:
        await client.aclose()
    return ProbeResult("redis", healthy=True)


async def probe_celery(celery_app: Any) -> ProbeResult:
    """Ask the workers to answer.

    `control.ping()` is a **synchronous, network-blocking** call. Calling it directly from this
    coroutine would block the event loop for its full timeout — for every concurrent user, not just
    the one hitting the health endpoint. That is the exact failure this codebase treats as CRITICAL
    (ADR-0005), and a health check that causes an outage would be a memorable way to learn it. Hence
    `asyncio.to_thread`.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS + 1):
            replies = await asyncio.to_thread(
                celery_app.control.ping, timeout=PROBE_TIMEOUT_SECONDS
            )
    except Exception as exc:
        log.warning("probe.celery.failed", error=type(exc).__name__)
        return ProbeResult("celery", healthy=False, detail=type(exc).__name__)

    if not replies:
        # Not an error — a silence. Which is the whole problem with background workers, and exactly
        # what this probe exists to turn into a red light.
        log.warning("probe.celery.no_workers")
        return ProbeResult("celery", healthy=False, detail="no workers responded")

    return ProbeResult("celery", healthy=True, detail=f"{len(replies)} worker(s)")
