"""`SqlAlchemyDailyCounter` and `SqlAlchemyDemandCounterQuery` against a real Postgres (slice 5.2,
T22, test-after).

The upsert counts, never commits on its own (the fact's transaction does), and the query reads a
closed range with absent pairs absent. "Same transaction as the fact" end to end is T30's and the held
specifications in `tests/integration/billing/test_pending_adapters.py`.
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.persistence.demand.daily_counter import (
    SqlAlchemyDailyCounter,
    SqlAlchemyDemandCounterQuery,
)


def _day() -> date:
    """A day no real run uses, one per test (`test_spend_ledger.py`'s reason)."""
    return date(2400, 1, 1) + timedelta(days=secrets.randbelow(3000))


async def test_increments_add_up_per_day_and_counter_and_the_query_reads_a_closed_range(
    session: AsyncSession,
) -> None:
    day = _day()
    counter = SqlAlchemyDailyCounter(session)
    for _ in range(3):
        await counter.increment(DemandCounter.SIGN_UPS, day)
    await counter.increment(DemandCounter.RUNS_FAILED, day)
    await counter.increment(DemandCounter.SIGN_UPS, day + timedelta(days=1))
    await counter.increment(DemandCounter.SIGN_UPS, day + timedelta(days=2))

    counts = await SqlAlchemyDemandCounterQuery(session).between(day, day + timedelta(days=1))

    assert counts == {
        (day, DemandCounter.SIGN_UPS): 3,
        (day, DemandCounter.RUNS_FAILED): 1,
        (day + timedelta(days=1), DemandCounter.SIGN_UPS): 1,
    }


async def test_an_increment_does_not_commit_on_its_own(session: AsyncSession) -> None:
    day = _day()
    await session.commit()
    await SqlAlchemyDailyCounter(session).increment(DemandCounter.PACK_CLICKS, day)
    await session.rollback()

    assert await SqlAlchemyDemandCounterQuery(session).between(day, day) == {}
