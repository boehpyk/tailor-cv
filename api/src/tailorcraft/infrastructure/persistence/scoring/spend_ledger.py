"""The spend ledger: `SqlSpendLedger` and `CommittingSpendLedger` (ADR-0041, slice 5.1).

**Two transactions, and no connection between them** (plan §0.2). The port promises each return is
durable, and `application/` may not name a commit (ADR-0002), so the use case's call order is the
boundary and `CommittingSpendLedger` is what makes it one. Its commit on a `True` reservation ends
transaction A: `AsyncSession.commit` hands the connection back to the pool, and the session checks one
out again only when next used. `RequestFitScore` next touches the session in `store`, after `assess`.
With `expire_on_commit=False` and no relationships on the mapped aggregates, nothing lazy-loads in
between.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.scoring.ports import SpendLedgerPort
from tailorcraft.domain.scoring.value_objects import MicroUsd
from tailorcraft.infrastructure.persistence.mapping.scoring.daily_spend import daily_spend_table

_table = daily_spend_table


class SqlSpendLedger:
    """One row per UTC day. Nothing here commits. `updated_at` is the database's `now()`: the port
    passes no instant, nothing reads the column back, and `TIMESTAMP(0)` keeps it whole-second."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_reserve(self, day: date, amount: MicroUsd, cap: MicroUsd) -> bool:
        """One atomic statement under the row's lock, so concurrency cannot overshoot; no row returned
        is a refusal (ADR-0041 decision 2).

        **The guard is not redundant.** `DO UPDATE … WHERE` is only evaluated on a conflict, so the day's
        first reservation would insert whatever it was given: past the cap, and past `cap = 0`, the off
        switch. `spent` is never negative, so `amount > cap` refuses on any day without asking.
        """
        if amount.value > cap.value:
            return False
        added = _table.c.spent_microusd + amount.value
        reserved = await self._session.execute(
            insert(_table)
            .values(day=day, spent_microusd=amount.value, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[_table.c.day],
                set_={"spent_microusd": added, "updated_at": func.now()},
                where=added <= cap.value,
            )
            .returning(_table.c.spent_microusd)
        )
        return reserved.first() is not None

    async def settle(self, day: date, reserved: MicroUsd, actual: MicroUsd) -> None:
        """Replace the reservation with the actual cost on `day`, floored at zero (decision 3)."""
        await self._session.execute(
            update(_table)
            .where(_table.c.day == day)
            .values(
                spent_microusd=func.greatest(
                    _table.c.spent_microusd + actual.value - reserved.value, 0
                ),
                updated_at=func.now(),
            )
        )


class CommittingSpendLedger:
    """The `SpendLedgerPort` the request path binds: **a reservation and a settlement commit.**

    `settle`'s commit is also what makes a stored score durable, so a row and its cost land together
    (AC-14). A refused reservation changed nothing and no call follows, so it has nothing to commit, and
    the request's session ends the transaction. **No commit when the inner call raises:** the caller's
    session rolls back and the reservation stands, the safe direction (S-15, S-16).

    Delegation, as `CommittingGuestWorkClaim` does.
    """

    def __init__(self, inner: SpendLedgerPort, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    async def try_reserve(self, day: date, amount: MicroUsd, cap: MicroUsd) -> bool:
        reserved = await self._inner.try_reserve(day, amount, cap)
        if reserved:
            await self._session.commit()
        return reserved

    async def settle(self, day: date, reserved: MicroUsd, actual: MicroUsd) -> None:
        await self._inner.settle(day, reserved, actual)
        await self._session.commit()


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance here, where it is defined. Never executed.
    def _assert_sql_implements_port(adapter: SqlSpendLedger) -> None:
        _: SpendLedgerPort = adapter

    def _assert_committing_implements_port(adapter: CommittingSpendLedger) -> None:
        _: SpendLedgerPort = adapter
