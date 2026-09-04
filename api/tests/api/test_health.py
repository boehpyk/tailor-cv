"""The health endpoints — and specifically, what they refuse to lie about.

The readiness endpoint exists because of a documented failure in the previous project: probing only
Postgres and Redis let a crash-looping background worker sit behind a green dashboard for an entire
session. So the interesting test here is not the happy path. It is the one asserting that a stack
with **no Celery worker running** reports 503 — because that is the exact scenario that used to
report 200 (ADR-0005).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient


async def test_liveness_answers_while_the_process_is_up(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"alive": True}


async def test_liveness_does_not_probe_dependencies(client: AsyncClient) -> None:
    """Liveness must stay cheap and dependency-free.

    Docker restarts a container that fails its liveness check. If liveness probed PostgreSQL, a
    brief database blip would kill every application container at once instead of letting them wait
    for the database to come back — turning a thirty-second outage into a restart storm.
    """
    response = await client.get("/health/live")

    assert "dependencies" not in response.json()


class _StubControl:
    def __init__(self, replies: list[dict[str, Any]] | None) -> None:
        self._replies = replies

    def ping(self, timeout: float = 1.0) -> list[dict[str, Any]] | None:
        return self._replies


class _StubCelery:
    def __init__(self, replies: list[dict[str, Any]] | None) -> None:
        self.control = _StubControl(replies)


@pytest.fixture
def _healthy_celery(app: FastAPI) -> None:
    """Pretend a worker is listening.

    Stubbed rather than started: booting a real worker inside the suite would make every test in
    this file depend on a background process, and the behaviour under test — "what does the endpoint
    report given N replies" — is fully determined by the reply list.
    """
    app.state.celery = _StubCelery([{"worker@host": {"ok": "pong"}}])


@pytest.mark.usefixtures("_healthy_celery")
async def test_readiness_reports_every_dependency_by_name(client: AsyncClient) -> None:
    """A bare {"status": "ok"} would look identical whether it probed three dependencies or none."""
    response = await client.get("/health/ready")
    body = response.json()

    assert set(body["dependencies"]) == {"postgres", "redis", "celery"}


@pytest.mark.usefixtures("_healthy_celery")
async def test_readiness_is_200_when_every_dependency_answers(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 200, body
    assert body["ready"] is True


async def test_readiness_is_503_when_no_celery_worker_responds(
    client: AsyncClient, app: FastAPI
) -> None:
    """The regression this endpoint exists for.

    A silent, stopped worker is indistinguishable from a healthy system to a check that only asks
    the datastores: the API answers, the database answers, and every export queues forever. Remove
    the Celery probe from `routers/health.py` and this test — and only this test — goes red.
    """
    app.state.celery = _StubCelery([])

    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["ready"] is False
    assert body["dependencies"]["celery"]["healthy"] is False
    # The other two are genuinely up, and the report says so rather than collapsing to one flag.
    assert body["dependencies"]["postgres"]["healthy"] is True
