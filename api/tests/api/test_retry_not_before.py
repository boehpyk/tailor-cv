"""API tests for `retry_not_before` on the three run shapes (slice 3.3, T4 RED; AC-1).

A run that failed `llm_rate_limited` carries `completed_at + 60 s` (ISO-8601 UTC, whole seconds);
every other reason, and every non-failed status, carries `null`. Parametrized over the enum itself,
so an eleventh reason is a red test here rather than a silent `null`. Runs are seeded through the
domain (`mark_failed` at a whole-second fake-clock instant) and the real repositories, never raw SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Literal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import ME_RUNS, mint_guest, new_client, register, seed_entry
from tests.integration.owners import queued_run, running_run, succeeded_run

Variant = TailoringFailureReason | Literal["queued", "running", "succeeded"]

_VARIANTS: list[Variant] = [*TailoringFailureReason, "queued", "running", "succeeded"]
_COOLDOWN = timedelta(seconds=60)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Rate limiters live in Redis, which the rollback never reaches."""


def _run_for(variant: Variant, owner: Owner, clock: FixedClock) -> TailoringRun:
    at = clock.now()
    if variant == "queued":
        return queued_run(owner, at)
    if variant == "running":
        return running_run(owner, at)
    if variant == "succeeded":
        return succeeded_run(owner, at)
    run = running_run(owner, at)
    run.mark_failed(variant, at + timedelta(seconds=7))
    run.release_events()
    return run


def _expected(variant: Variant, run: TailoringRun) -> str | None:
    if variant is not TailoringFailureReason.LLM_RATE_LIMITED:
        return None
    assert run.completed_at is not None
    return (run.completed_at + _COOLDOWN).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_instant(actual: object, expected: str | None) -> None:
    """The wire may spell UTC as `Z` or `+00:00`; the instant and the whole second may not vary."""
    if expected is None:
        assert actual is None
        return
    assert isinstance(actual, str), f"expected {expected}, got {actual!r}"
    assert actual.replace("+00:00", "Z") == expected
    assert "." not in actual, "whole seconds only"


@pytest.mark.parametrize("variant", _VARIANTS, ids=str)
async def test_guest_detail_carries_retry_not_before(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    variant: Variant,
) -> None:
    owner = await mint_guest(client, session)
    run = _run_for(variant, owner, clock)
    expected = _expected(variant, run)
    entry = await seed_entry(session, settings, owner, at=clock.now(), run=run, ready_formats=())

    response = await client.get(f"/api/tailoring-runs/{entry.run_id.value}")

    assert response.status_code == 200, response.text
    _same_instant(response.json()["retry_not_before"], expected)


@pytest.mark.parametrize("variant", _VARIANTS, ids=str)
async def test_guest_list_carries_retry_not_before(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    variant: Variant,
) -> None:
    owner = await mint_guest(client, session)
    run = _run_for(variant, owner, clock)
    expected = _expected(variant, run)
    entry = await seed_entry(session, settings, owner, at=clock.now(), run=run, ready_formats=())

    response = await client.get("/api/tailoring-runs")

    assert response.status_code == 200, response.text
    (item,) = [i for i in response.json()["items"] if i["id"] == str(entry.run_id.value)]
    _same_instant(item["retry_not_before"], expected)


@pytest.mark.parametrize("variant", _VARIANTS, ids=str)
async def test_account_detail_carries_retry_not_before(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    variant: Variant,
) -> None:
    account = await register(client, settings)
    run = _run_for(variant, account.owner, clock)
    expected = _expected(variant, run)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now(), run=run, ready_formats=()
    )

    response = await client.get(entry.run_url, headers=account.headers)

    assert response.status_code == 200, response.text
    _same_instant(response.json()["retry_not_before"], expected)


@pytest.mark.parametrize("variant", _VARIANTS, ids=str)
async def test_history_entry_carries_retry_not_before(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    variant: Variant,
) -> None:
    account = await register(client, settings)
    run = _run_for(variant, account.owner, clock)
    expected = _expected(variant, run)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now(), run=run, ready_formats=()
    )

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    (item,) = [i for i in response.json()["items"] if i["id"] == str(entry.run_id.value)]
    _same_instant(item["retry_not_before"], expected)
