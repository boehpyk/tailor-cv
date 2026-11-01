"""AC-41 under load (slice 5.1, T46): a burst against a low cap, and the pool beside it. Fake scorer;
no test calls Gemini.

Run as an ordinary test (`make test`) it is a smoke gate on the dev image. **The numbers that count
are measured on the production image** (`docker build --target production`; LESSONS: the dev image is
`--reload`), by running this file's two tests there with the recorded line printed. Each test prints
one `T46 ...` line with its figures; `-s` shows it.

- **The ledger never goes over (AC-13 at scale):** 50 accounts `POST` at once against a cap that fits
  a handful of reservations. A sampler reads the day's `spent_microusd` every few milliseconds while
  the scorer is suspended; no sample, and not the final figure, passes the cap. The rest are refused
  503 `scoring_busy` and the scorer is called only for the admitted.
- **The pool is never exhausted:** while all 50 requests are inside `assess`, `GET`s on the same app
  must keep answering 200 with p95 <= 300 ms (AC-41), and the pool must never be seen with every
  connection checked out. The pool timeout is 30 s, so a held connection would show as a `GET` that
  waits, or as a 500 `TimeoutError` after 30 s; the 1 s ceiling below is what notices it quickly.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.me_support import Account, new_client, override_settings
from tests.api.test_fit_score_held_resources import seed_pair, spent
from tests.integration.scoring.support import assessed

pytestmark = pytest.mark.slow

URL = "/api/me/fit-scores"
BURST = 50
CAP_MICROUSD = 5_000
READERS = 4  # concurrent GET loops
HOLD_SECONDS = 0.5  # how long the fake scorer is "thinking": the window the GETs must fit inside


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The IP limiter lives in Redis, which no rollback reaches."""


class SlowScorer:
    """Suspends every call. With `gather=N` no call returns until N are inside at once, then each
    holds for HOLD_SECONDS: `holding` is True only in that window, when the pool should be idle."""

    def __init__(self, gather: int | None = None) -> None:
        self.calls = 0
        self.inside = 0
        self.peak_inside = 0
        self._gather = gather
        self._all_in = asyncio.Event()
        self.holding = False

    async def assess(self, cv: object, posting: object) -> object:
        self.calls += 1
        self.inside += 1
        self.peak_inside = max(self.peak_inside, self.inside)
        try:
            if self._gather is not None:
                if self.inside >= self._gather:
                    self._all_in.set()
                    self.holding = True
                await asyncio.wait_for(self._all_in.wait(), 30)
            await asyncio.sleep(HOLD_SECONDS)
        finally:
            self.inside -= 1
            self.holding = False
        return assessed(62)


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path / "uploads")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scoring_daily_spend"))
    try:
        yield w
    finally:
        await w.cleanup()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM scoring_daily_spend"))


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass
class Burst:
    statuses: list[int]
    ledger: list[int]  # the day's spent_microusd, sampled
    pool: list[int]  # connections checked out, sampled
    get_ms: list[float]  # every GET during the burst
    hold_ms: list[float]  # the GETs made while all of them were inside the scorer
    get_statuses: list[int]
    hold_pool: list[int]  # connections checked out in that window


async def _burst(
    world: World, engine: AsyncEngine, scorer: SlowScorer, *, cap_microusd: int
) -> Burst:
    """Fire BURST POSTs; meanwhile sample the ledger and the pool and time concurrent GETs."""
    override_settings(
        world.app,
        world.settings,
        fit_score_daily_spend_cap_usd=Decimal(cap_microusd) / 1_000_000,
        fit_score_rate_limit_per_ip_per_hour=10_000,
        login_rate_limit_per_ip_per_hour=10_000,
    )
    world.app.state.fit_scorer = scorer
    accounts = [await world.account() for _ in range(BURST)]
    pairs = [await seed_pair(engine, a.owner) for a in accounts]
    ledger_samples: list[int] = []
    pool_samples: list[int] = []
    get_ms: list[float] = []
    hold_ms: list[float] = []
    hold_pool: list[int] = []
    get_statuses: list[int] = []
    done = asyncio.Event()

    async def sampler() -> None:
        while not done.is_set():
            ledger_samples.append(await spent(engine))
            checked_out = engine.pool.checkedout()  # type: ignore[attr-defined]
            pool_samples.append(checked_out)
            if scorer.holding:
                hold_pool.append(checked_out)
            await asyncio.sleep(0.005)

    async def reader(client: AsyncClient, who: Account, body: dict[str, str]) -> None:
        while not done.is_set():
            started = time.perf_counter()
            holding = scorer.holding
            response = await client.get(URL, params=body, headers=who.headers)
            elapsed = (time.perf_counter() - started) * 1000
            get_ms.append(elapsed)
            if holding and scorer.holding:
                hold_ms.append(elapsed)
            get_statuses.append(response.status_code)
            await asyncio.sleep(0.01)

    async with new_client(world.app) as client:
        sample_task = asyncio.ensure_future(sampler())
        read_tasks = [
            asyncio.ensure_future(reader(client, a, b))
            for a, b in list(zip(accounts, pairs, strict=True))[:READERS]
        ]
        try:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.post(URL, json=b, headers=a.headers)
                        for a, b in zip(accounts, pairs, strict=True)
                    )
                ),
                60,
            )
        finally:
            done.set()
            await asyncio.gather(sample_task, *read_tasks)
        statuses = [r.status_code for r in responses]
    ledger_samples.append(await spent(engine))
    return Burst(statuses, ledger_samples, pool_samples, get_ms, hold_ms, get_statuses, hold_pool)


async def test_a_burst_of_50_against_a_low_cap_never_takes_the_ledger_over_it(
    world: World, engine: AsyncEngine
) -> None:
    scorer = SlowScorer()
    burst = await _burst(world, engine, scorer, cap_microusd=CAP_MICROUSD)
    statuses, ledger = burst.statuses, burst.ledger
    admitted, refused = statuses.count(201), statuses.count(503)
    print(
        f"T46 ledger: {BURST} POSTs, cap {CAP_MICROUSD} micro-USD: {admitted} admitted, "
        f"{refused} refused 503; scorer called {scorer.calls}; peak in assess {scorer.peak_inside}; "
        f"max sampled ledger {max(ledger)} over {len(ledger)} samples; final {ledger[-1]}"
    )
    assert set(statuses) <= {201, 503}, statuses
    assert refused >= 1, "the cap never bit: raise the burst or lower the cap"
    assert admitted >= 1
    assert scorer.calls == admitted  # a refused request never reaches the scorer
    assert max(ledger) <= CAP_MICROUSD, f"ledger passed the cap: {max(ledger)}"
    assert ledger[-1] <= CAP_MICROUSD


async def test_concurrent_gets_beside_50_suspended_scores_never_wait_for_the_pool(
    world: World, engine: AsyncEngine
) -> None:
    scorer = SlowScorer(gather=BURST)
    burst = await _burst(world, engine, scorer, cap_microusd=10_000_000)
    ceiling = engine.pool.size() + engine.pool._max_overflow  # type: ignore[attr-defined]
    ramp_p95, ramp_worst = _p95(burst.get_ms), max(burst.get_ms)
    hold = burst.hold_ms
    print(
        f"T46 pool (size {ceiling}): {scorer.peak_inside} scores inside the scorer at once, "
        f"all {burst.statuses.count(201)}/{BURST} 201. Whole burst: {len(burst.get_ms)} GETs, "
        f"p95 {ramp_p95:.1f} ms, max {ramp_worst:.1f} ms, peak checked out {max(burst.pool)}. "
        f"While all {BURST} were inside: {len(hold)} GETs, p50 {statistics.median(hold):.1f} ms, "
        f"p95 {_p95(hold):.1f} ms, max {max(hold):.1f} ms, checked out {sorted(set(burst.hold_pool))}"
    )
    assert burst.statuses == [201] * BURST, burst.statuses
    assert scorer.peak_inside == BURST, "the burst was not larger than the pool: nothing proven"
    assert ceiling < BURST
    assert len(hold) >= 20, "the GETs did not overlap the hold"
    assert set(burst.get_statuses) == {200}, set(burst.get_statuses)  # no pool timeout, ever
    # The only connections out are the readers' own GETs: a POST holding one across `assess` would
    # put all ten out.
    assert max(burst.hold_pool) <= READERS, f"a POST holds a connection: {set(burst.hold_pool)}"
    assert ramp_worst < 5_000, f"a GET waited {ramp_worst:.0f} ms: it queued for a connection"
    assert _p95(hold) <= 300, f"AC-41: GET p95 {_p95(hold):.1f} ms while the scores are held"
