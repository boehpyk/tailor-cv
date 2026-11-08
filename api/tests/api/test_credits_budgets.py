"""AC-40 (slice 5.2, T38): the credit ledger's cost on the request path. No test calls Gemini.

Run as an ordinary test (`make test`) it is a smoke gate on the dev image. **The numbers that count
are measured on the production image** (`docker build --target production`; `tailorcraft-api:local`
is the dev image, `--reload` and all), by running this file there and reading the `T38 ...` lines
(`-s`). The charge's delta against `main` is measured by running the same flow, minus the ledger
rows, on `main`'s image; the figures are in the task list.

- **The tailoring `POST` with a 200-entry history** is timed sequentially end to end (p95 < 300 ms).
  `ChargeCreditForRun.__call__` is wrapped with a timer, so the charge's own share (lock, load of
  the history, save) is a number of its own: with the other statements of the request unchanged, it
  is the delta against `main` (p95 < 30 ms) without a second tree. Between POSTs the run and its
  charge are deleted (outside the timer), so every POST meets exactly the 200 entries and an
  idle account.
- **`GET /api/me/credits` with 100 entries** and **`GET /api/admin/demand-counters` over 92 days**
  (every counter, every day) are timed the same way against the 300 ms budget.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.application.billing.charge_credit_for_run import ChargeCreditForRun
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.credits_support import (
    ADMIN_COUNTERS,
    COUNTER_NAMES,
    ME_CREDITS,
    ME_RUNS,
    reset_ledger,
)
from tests.api.me_support import new_client, override_settings
from tests.api.test_fit_score_held_resources import seed_pair

pytestmark = pytest.mark.slow

POSTS = 100
HISTORY = 200
BUDGET_MS = 300
CHARGE_BUDGET_MS = 30
DAYS = 92
FIRST_DAY = date(2020, 1, 1)  # no other test writes 2020-01..04


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The tailoring and login limiters live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path / "uploads")
    override_settings(
        w.app,
        w.settings,
        tailoring_rate_limit_per_hour=10_000,
        tailoring_rate_limit_per_ip_per_hour=10_000,
        login_rate_limit_per_ip_per_hour=10_000,
    )
    try:
        yield w
    finally:
        await w.cleanup()


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _summary(values: list[float]) -> str:
    return f"p50 {statistics.median(values):.1f} ms, p95 {_p95(values):.1f} ms, max {max(values):.1f} ms"


async def _timed_gets(get: Callable[[], Awaitable[Any]]) -> list[float]:
    elapsed: list[float] = []
    for _ in range(POSTS):
        started = time.perf_counter()
        response = await get()
        elapsed.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200, response.text
    return elapsed


async def test_post_tailoring_run_with_a_200_entry_history_is_300_ms_p95_and_the_charge_30(
    world: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = await world.account()
    body = await seed_pair(engine, who.owner)
    user = who.user_id.value
    async with engine.begin() as conn:
        # 1 opening + 199 one-credit grants: a history of 200 with a balance to spend.
        await reset_ledger(conn, user, extra_rows=HISTORY - 1)

    charge_ms: list[float] = []
    real_call = ChargeCreditForRun.__call__

    async def timed_call(self: ChargeCreditForRun, cmd: Any) -> Any:
        started = time.perf_counter()
        try:
            return await real_call(self, cmd)
        finally:
            charge_ms.append((time.perf_counter() - started) * 1000)

    monkeypatch.setattr(ChargeCreditForRun, "__call__", timed_call)

    total_ms: list[float] = []
    async with new_client(world.app) as client:
        for _ in range(POSTS):
            started = time.perf_counter()
            response = await client.post(ME_RUNS, json=body, headers=who.headers)
            total_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 202, response.text
            async with engine.begin() as conn:  # back to an idle account with exactly 200 entries
                await conn.execute(
                    text(
                        "DELETE FROM billing_credit_entry WHERE tailoring_run_id IN "
                        "(SELECT id FROM tailoring_run WHERE user_id = :u)"
                    ),
                    {"u": user},
                )
                await conn.execute(
                    text("DELETE FROM tailoring_run WHERE user_id = :u"), {"u": user}
                )
    print(
        f"T38 tailoring POST, {HISTORY}-entry history: {POSTS} sequential, {_summary(total_ms)}; "
        f"charge alone: {_summary(charge_ms)}"
    )
    assert len(charge_ms) == POSTS, "the charge was not on the request path"
    assert _p95(total_ms) < BUDGET_MS, f"AC-40: POST p95 {_p95(total_ms):.1f} ms"
    assert _p95(charge_ms) < CHARGE_BUDGET_MS, f"AC-40: charge p95 {_p95(charge_ms):.1f} ms"


async def test_get_credits_with_100_entries_is_300_ms_p95(
    world: World, engine: AsyncEngine
) -> None:
    who = await world.account()
    async with engine.begin() as conn:
        await reset_ledger(conn, who.user_id.value, extra_rows=99)
    async with new_client(world.app) as client:
        elapsed = await _timed_gets(lambda: client.get(ME_CREDITS, headers=who.headers))
    print(f"T38 GET credits, 100 entries: {POSTS} sequential, {_summary(elapsed)}")
    assert _p95(elapsed) < BUDGET_MS, f"GET credits p95 {_p95(elapsed):.1f} ms"


async def test_get_demand_counters_over_92_days_is_300_ms_p95(
    world: World, engine: AsyncEngine
) -> None:
    admin = await world.account()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM demand_daily_counter WHERE day < '2020-06-01'"))
        await conn.execute(
            text("UPDATE identity_user SET role = 'admin' WHERE id = :i"),
            {"i": admin.user_id.value},
        )
        for offset in range(DAYS):
            for name in COUNTER_NAMES:
                await conn.execute(
                    text(
                        "INSERT INTO demand_daily_counter (day, counter, count) VALUES (:d, :c, :n)"
                    ),
                    {"d": FIRST_DAY + timedelta(days=offset), "c": name, "n": offset + 1},
                )
    last = FIRST_DAY + timedelta(days=DAYS - 1)
    try:
        async with new_client(world.app) as client:
            elapsed = await _timed_gets(
                lambda: client.get(
                    ADMIN_COUNTERS,
                    params={"from": str(FIRST_DAY), "to": str(last)},
                    headers=admin.headers,
                )
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM demand_daily_counter WHERE day < '2020-06-01'"))
    print(
        f"T38 GET demand counters, {DAYS} days x {len(COUNTER_NAMES)}: {POSTS} sequential, {_summary(elapsed)}"
    )
    assert _p95(elapsed) < BUDGET_MS, f"demand counters p95 {_p95(elapsed):.1f} ms"
