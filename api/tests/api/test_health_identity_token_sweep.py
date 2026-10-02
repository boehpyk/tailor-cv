"""`/health/ready`'s `jobs.identity_token_sweep` (slice 2.5, T23, test-after; ADR-0019 amendment (a),
AC-43's endpoint half).

The fact reported is the **backlog**, asked through the sweep's own port (`count_overdue`) so it is the
predicate the sweep deletes by and not a second derivation of it. This job has no heartbeat, so the key
set is exactly `scheduled`, `overdue`, `detail` (a `JobStatus`'s four permanent `None`s would read as
"never ran"), and, like `guest_purge`, the block can degrade a field but never the status code.

The count is replaced at the adapter's method (`count_overdue`) — the probe's own error floor is what a
raising replacement exercises. The *rows* behind the count are proven against real PostgreSQL in
`tests/integration/persistence/test_expired_identity_tokens_adapter.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tailorcraft.infrastructure.api.deps import get_clock
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.retention.expired_identity_tokens import (
    SqlAlchemyExpiredIdentityTokens,
)
from tailorcraft.infrastructure.tasks.app import IDENTITY_TOKEN_SWEEP_OVERDUE_GRACE_SECONDS


class _StubControl:
    def ping(self, timeout: float = 1.0) -> list[dict[str, Any]] | None:
        return [{"worker@host": {"ok": "pong"}}]


class _StubCelery:
    control = _StubControl()


@pytest.fixture(autouse=True)
def _healthy_celery(app: FastAPI) -> None:
    app.state.celery = _StubCelery()


def _sweep(body: dict[str, Any]) -> dict[str, Any]:
    return dict(body["jobs"]["identity_token_sweep"])


async def test_the_key_set_is_exactly_scheduled_overdue_and_detail(client: AsyncClient) -> None:
    body = (await client.get("/health/ready")).json()

    assert set(body["jobs"]["identity_token_sweep"]) == {"scheduled", "overdue", "detail"}
    assert "guest_purge" in body["jobs"], "the guest purge block must still be there"


async def test_a_healthy_count_is_reported_as_a_fact_with_no_detail(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    sweep = _sweep(response.json())

    assert response.status_code == 200
    assert sweep["scheduled"] is True
    assert isinstance(sweep["overdue"], int)
    assert sweep["overdue"] >= 0
    assert sweep["detail"] is None


async def test_the_probe_asks_the_ports_own_count_at_now_with_a_two_interval_grace(
    client: AsyncClient,
    app: FastAPI,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[datetime, timedelta]] = []

    async def _count(
        self: SqlAlchemyExpiredIdentityTokens, as_of: datetime, grace: timedelta
    ) -> int:
        asked.append((as_of, grace))
        return 7

    monkeypatch.setattr(SqlAlchemyExpiredIdentityTokens, "count_overdue", _count)
    app.dependency_overrides[get_clock] = lambda: clock

    sweep = _sweep((await client.get("/health/ready")).json())

    assert sweep["overdue"] == 7
    assert sweep["detail"] is None
    assert asked == [(clock.now(), timedelta(seconds=IDENTITY_TOKEN_SWEEP_OVERDUE_GRACE_SECONDS))]
    assert timedelta(seconds=IDENTITY_TOKEN_SWEEP_OVERDUE_GRACE_SECONDS) == timedelta(hours=2)


async def test_a_failed_count_degrades_only_that_field_and_never_the_status_code(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "MARKER-postgres-quoted-a-row-3c9e"

    async def _boom(
        self: SqlAlchemyExpiredIdentityTokens, as_of: datetime, grace: timedelta
    ) -> int:
        raise RuntimeError(marker)

    monkeypatch.setattr(SqlAlchemyExpiredIdentityTokens, "count_overdue", _boom)

    response = await client.get("/health/ready")
    body = response.json()
    sweep = _sweep(body)

    assert response.status_code == 200, body
    assert body["ready"] is True
    assert sweep["overdue"] is None
    assert sweep["detail"] == "overdue: RuntimeError"
    assert sweep["scheduled"] is True
    assert marker not in response.text


async def test_a_backlog_never_makes_the_endpoint_unready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 decision 2: a background hygiene job that is behind does not make this process unable
    to answer a request; pulling the app out of service over it would fail a deploy's readiness gate
    for a job the deploy just restarted."""

    async def _huge(
        self: SqlAlchemyExpiredIdentityTokens, as_of: datetime, grace: timedelta
    ) -> int:
        return 10_000_000

    monkeypatch.setattr(SqlAlchemyExpiredIdentityTokens, "count_overdue", _huge)

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert _sweep(response.json())["overdue"] == 10_000_000
