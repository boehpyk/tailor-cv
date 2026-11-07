"""`SqlAlchemyDailyCounter` and `SqlAlchemyDemandCounterQuery` (ADR-0043, plan §0.9).

The write is one upsert on the **caller's** session and never commits: the fact's own transaction
commits the count with the fact, or rolls both back. The row lock it takes is held to that commit, so
a counter is a hot row on a busy day (plan §8, risk 4; irrelevant at this product's traffic).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.persistence.mapping.demand.daily_counter import (
    daily_counter_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.shared.counters import DailyCounterPort, DemandCounterQuery

_table = daily_counter_table


class SqlAlchemyDailyCounter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def increment(self, counter: DemandCounter, day: date) -> None:
        """`INSERT … ON CONFLICT (day, counter) DO UPDATE SET count = count + 1`."""
        await self._session.execute(
            insert(_table)
            .values(day=day, counter=counter.value, count=1)
            .on_conflict_do_update(
                index_elements=[_table.c.day, _table.c.counter],
                set_={"count": _table.c.count + 1},
            )
        )


class SqlAlchemyDemandCounterQuery:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def between(
        self, from_day: date, to_day: date
    ) -> Mapping[tuple[date, DemandCounter], int]:
        """One `SELECT … WHERE day BETWEEN`, served by the primary key's leading column."""
        rows = await self._session.execute(
            select(_table.c.day, _table.c.counter, _table.c.count).where(
                _table.c.day.between(from_day, to_day)
            )
        )
        return {(day, DemandCounter(counter)): count for day, counter, count in rows}


if TYPE_CHECKING:

    def _assert_implements_ports(
        counter: SqlAlchemyDailyCounter, query: SqlAlchemyDemandCounterQuery
    ) -> None:
        _: DailyCounterPort = counter
        __: DemandCounterQuery = query
