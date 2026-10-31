"""`SqlSpendLedger` and `CommittingSpendLedger` against a real Postgres (slice 5.1, T20, ADR-0041).

**Sequential rules** run in the rolled-back `session`; **AC-13** (N concurrent requests never bring the
ledger above the cap) runs on real, independent connections with a low cap, so the day's *first*
reservation is itself a race (two `INSERT`s for an absent row).

The cap rules are written from the spec and the ADR, never from the statement:

- A reservation is admitted when `spent + amount <= cap` and refused otherwise, leaving the day's
  figure as it was. A smaller one can still fit after a larger one was refused (S-10).
- **`cap = 0` refuses everything**: it is the documented off switch. The upsert's `WHERE` is only
  evaluated on a conflict, so the day's first reservation needs its own guard; `test_..._first_...`
  is the one that notices if that guard goes.
- `settle` replaces the reservation with the actual cost, floored at zero. **With no row for the day
  it is a no-op in the real adapter.** `RecordingSpendLedger` used to create the entry instead: a
  fidelity gap that hid a wrong-day `settle` (the real adapter would lose the cost silently while the
  fake showed it recorded). The fake now agrees, and `test_the_fake_...` pins that.

**Mutation record (T20), restored byte-exact** (`git diff --stat -- api/src` empty):

- The upsert's `where=added <= cap.value` removed: `test_concurrent_reservations_never_pass_the_cap`
  red (more admitted than the cap allows), `test_a_reservation_that_would_pass_the_cap_is_refused_and_changes_nothing`
  red (4 red in all).
- The `amount.value > cap.value` guard removed: `test_a_first_reservation_above_the_cap_is_refused`
  and `test_a_cap_of_zero_refuses_every_reservation_on_every_attempt` red (4 red in all).
- `CommittingSpendLedger.try_reserve`'s commit removed: `test_a_granted_reservation_is_committed`
  red.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.scoring.value_objects import MicroUsd
from tailorcraft.infrastructure.persistence.scoring.spend_ledger import (
    CommittingSpendLedger,
    SqlSpendLedger,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import assert_test_database
from tests.integration.fakes import RecordingSpendLedger


def _day() -> date:
    """A day no real run uses, and one per test, so a committed row can never meet another test's."""
    return date(2400, 1, 1) + timedelta(days=secrets.randbelow(3000))


@pytest.fixture
def day() -> date:
    return _day()


async def _spent(session: AsyncSession, day: date) -> int | None:
    row = (
        await session.execute(
            text("SELECT spent_microusd FROM scoring_daily_spend WHERE day = :d"), {"d": day}
        )
    ).first()
    return None if row is None else int(row[0])


# --- try_reserve: the cap -------------------------------------------------------------------------


async def test_the_first_reservation_of_a_day_creates_the_row(
    session: AsyncSession, day: date
) -> None:
    ledger = SqlSpendLedger(session)

    assert await ledger.try_reserve(day, MicroUsd(300), MicroUsd(1000)) is True

    assert await _spent(session, day) == 300


async def test_a_reservation_exactly_up_to_the_cap_is_admitted(
    session: AsyncSession, day: date
) -> None:
    ledger = SqlSpendLedger(session)
    assert await ledger.try_reserve(day, MicroUsd(600), MicroUsd(1000)) is True

    assert await ledger.try_reserve(day, MicroUsd(400), MicroUsd(1000)) is True

    assert await _spent(session, day) == 1000


async def test_a_reservation_that_would_pass_the_cap_is_refused_and_changes_nothing(
    session: AsyncSession, day: date
) -> None:
    ledger = SqlSpendLedger(session)
    await ledger.try_reserve(day, MicroUsd(600), MicroUsd(1000))

    assert await ledger.try_reserve(day, MicroUsd(401), MicroUsd(1000)) is False

    assert await _spent(session, day) == 600


async def test_a_smaller_reservation_still_fits_after_a_larger_one_was_refused(
    session: AsyncSession, day: date
) -> None:
    """S-10: a long CV can be refused while a short one fits."""
    ledger = SqlSpendLedger(session)
    await ledger.try_reserve(day, MicroUsd(600), MicroUsd(1000))
    assert await ledger.try_reserve(day, MicroUsd(500), MicroUsd(1000)) is False

    assert await ledger.try_reserve(day, MicroUsd(300), MicroUsd(1000)) is True

    assert await _spent(session, day) == 900


async def test_a_first_reservation_above_the_cap_is_refused_and_creates_no_row(
    session: AsyncSession, day: date
) -> None:
    """The upsert's `WHERE` is not evaluated for an insert, so an absent day would admit anything."""
    ledger = SqlSpendLedger(session)

    assert await ledger.try_reserve(day, MicroUsd(1001), MicroUsd(1000)) is False

    assert await _spent(session, day) is None


async def test_a_cap_of_zero_refuses_every_reservation_on_every_attempt(
    session: AsyncSession, day: date
) -> None:
    """`cap = 0` is the off switch. The first call of the day is the one an upsert would let through."""
    ledger = SqlSpendLedger(session)

    attempts = [await ledger.try_reserve(day, MicroUsd(1), MicroUsd(0)) for _ in range(3)]

    assert attempts == [False, False, False]
    assert await _spent(session, day) is None


async def test_days_are_independent(session: AsyncSession, day: date) -> None:
    ledger = SqlSpendLedger(session)
    tomorrow = day + timedelta(days=1)
    await ledger.try_reserve(day, MicroUsd(900), MicroUsd(1000))

    assert await ledger.try_reserve(tomorrow, MicroUsd(900), MicroUsd(1000)) is True

    assert (await _spent(session, day), await _spent(session, tomorrow)) == (900, 900)


async def test_updated_at_is_a_whole_second(session: AsyncSession, day: date) -> None:
    ledger = SqlSpendLedger(session)
    await ledger.try_reserve(day, MicroUsd(1), MicroUsd(10))
    await ledger.settle(day, MicroUsd(1), MicroUsd(1))

    stamped: datetime = (
        await session.execute(
            text("SELECT updated_at FROM scoring_daily_spend WHERE day = :d"), {"d": day}
        )
    ).scalar_one()

    assert stamped.microsecond == 0


# --- settle ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reserved", "actual", "expected"),
    [(500, 200, 700), (500, 500, 1000), (500, 900, 1400), (2000, 0, 0)],
)
async def test_settle_replaces_the_reservation_with_the_actual_cost_floored_at_zero(
    session: AsyncSession, day: date, reserved: int, actual: int, expected: int
) -> None:
    """The day holds 1000. Settling moves the figure by `actual - reserved`; the last case would go
    negative, so it floors at zero instead of breaking the CHECK."""
    ledger = SqlSpendLedger(session)
    await ledger.try_reserve(day, MicroUsd(1000), MicroUsd(10_000))

    await ledger.settle(day, MicroUsd(reserved), MicroUsd(actual))

    assert await _spent(session, day) == expected


async def test_settle_with_no_row_for_the_day_changes_nothing(
    session: AsyncSession, day: date
) -> None:
    """A no-op, not an entry: an `UPDATE` finds no row. (The reservation always created the day's row
    before a settle, so reaching this means a wrong day or a deleted row, and the adapter does not
    invent a figure for it.)"""
    await SqlSpendLedger(session).settle(day, MicroUsd(500), MicroUsd(900))

    assert await _spent(session, day) is None


async def test_the_fake_settle_with_no_row_for_the_day_changes_nothing_either() -> None:
    """Fidelity: the fake `RecordingSpendLedger` agrees with the adapter above. It used to create an
    entry (`max(0 + actual - reserved, 0)`), so a use case settling on the wrong day looked correct
    in every application test and lost the cost in production."""
    ledger = RecordingSpendLedger()
    day = date(2026, 9, 4)

    await ledger.settle(day, MicroUsd(500), MicroUsd(900))

    assert ledger.spent == {}
    assert ledger.settlements == [(day, MicroUsd(500), MicroUsd(900))]


# --- CommittingSpendLedger -------------------------------------------------------------------------


def _count_commits(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    calls: list[str] = []
    original = session.commit

    async def counting() -> None:
        calls.append("commit")
        await original()

    monkeypatch.setattr(session, "commit", counting)
    return lambda: len(calls)


async def test_a_granted_reservation_is_committed(
    session: AsyncSession, day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = _count_commits(session, monkeypatch)
    ledger = CommittingSpendLedger(SqlSpendLedger(session), session)

    assert await ledger.try_reserve(day, MicroUsd(100), MicroUsd(1000)) is True

    assert commits() == 1


async def test_a_refused_reservation_commits_nothing(
    session: AsyncSession, day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = _count_commits(session, monkeypatch)
    ledger = CommittingSpendLedger(SqlSpendLedger(session), session)

    assert await ledger.try_reserve(day, MicroUsd(100), MicroUsd(0)) is False

    assert commits() == 0


async def test_a_settlement_is_committed(
    session: AsyncSession, day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = CommittingSpendLedger(SqlSpendLedger(session), session)
    await ledger.try_reserve(day, MicroUsd(100), MicroUsd(1000))
    commits = _count_commits(session, monkeypatch)

    await ledger.settle(day, MicroUsd(100), MicroUsd(40))

    assert commits() == 1
    assert await _spent(session, day) == 40


async def test_a_failing_settlement_commits_nothing(
    session: AsyncSession, day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-16: the reservation stands, which is the safe direction."""

    class Down(Exception):
        pass

    class FailingInner:
        async def try_reserve(self, day: date, amount: MicroUsd, cap: MicroUsd) -> bool:
            raise Down

        async def settle(self, day: date, reserved: MicroUsd, actual: MicroUsd) -> None:
            raise Down

    commits = _count_commits(session, monkeypatch)
    ledger = CommittingSpendLedger(FailingInner(), session)

    with pytest.raises(Down):
        await ledger.settle(day, MicroUsd(1), MicroUsd(1))
    with pytest.raises(Down):
        await ledger.try_reserve(day, MicroUsd(1), MicroUsd(10))

    assert commits() == 0


# --- AC-13: real concurrent sessions, a low cap ----------------------------------------------------


@pytest_asyncio.fixture
async def committed_day(settings: Settings, engine: AsyncEngine) -> AsyncIterator[date]:
    """A day whose row (if any) is deleted afterwards: these tests commit for real."""
    assert_test_database(settings)
    chosen = _day()
    try:
        yield chosen
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM scoring_daily_spend WHERE day = :d"), {"d": chosen}
            )


async def _reserve_together(
    engine: AsyncEngine, day: date, amounts: list[int], cap: int
) -> list[bool]:
    """Every request on its own session and connection, released at once by a barrier, each ending in
    the commit the production wrapper makes (so the row lock is held across the real wait)."""
    barrier = asyncio.Barrier(len(amounts))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one(amount: int) -> bool:
        async with factory() as session:
            ledger = CommittingSpendLedger(SqlSpendLedger(session), session)
            await barrier.wait()
            return await ledger.try_reserve(day, MicroUsd(amount), MicroUsd(cap))

    return list(await asyncio.wait_for(asyncio.gather(*(one(a) for a in amounts)), 30.0))


async def _committed_spent(engine: AsyncEngine, day: date) -> int | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT spent_microusd FROM scoring_daily_spend WHERE day = :d"), {"d": day}
            )
        ).first()
    return None if row is None else int(row[0])


async def test_concurrent_reservations_never_pass_the_cap(
    engine: AsyncEngine, committed_day: date
) -> None:
    """AC-13. Twelve requests of 100 against a cap of 500 on a day with no row yet: exactly five are
    admitted and the committed figure is exactly the cap. Without the upsert's `WHERE` all twelve are
    admitted and the figure is 1200."""
    granted = await _reserve_together(engine, committed_day, [100] * 12, cap=500)

    assert sum(granted) == 5
    assert await _committed_spent(engine, committed_day) == 500


async def test_concurrent_reservations_of_unequal_size_never_pass_the_cap(
    engine: AsyncEngine, committed_day: date
) -> None:
    amounts = [300, 300, 300, 300, 300, 300, 120, 120, 120, 120]
    granted = await _reserve_together(engine, committed_day, amounts, cap=1000)

    admitted = sum(a for a, ok in zip(amounts, granted, strict=True) if ok)
    spent = await _committed_spent(engine, committed_day)
    assert spent == admitted
    assert spent is not None
    assert spent <= 1000
    # Nothing that fit was turned away: what is left is smaller than every refused amount.
    refused = [a for a, ok in zip(amounts, granted, strict=True) if not ok]
    assert all(1000 - spent < a for a in refused), (spent, refused)


async def test_concurrent_first_reservations_with_a_cap_of_zero_all_refuse(
    engine: AsyncEngine, committed_day: date
) -> None:
    granted = await _reserve_together(engine, committed_day, [1] * 8, cap=0)

    assert not any(granted)
    assert await _committed_spent(engine, committed_day) is None


async def test_concurrent_reservations_are_not_lost_when_all_fit(
    engine: AsyncEngine, committed_day: date
) -> None:
    """The control for the cap tests: with room for everyone, every request is admitted and none of
    the twelve increments is lost to the race on the day's first insert."""
    granted = await _reserve_together(engine, committed_day, [100] * 12, cap=10_000)

    assert all(granted)
    assert await _committed_spent(engine, committed_day) == 1200


async def test_a_committed_reservation_is_visible_to_another_connection_before_settle(
    engine: AsyncEngine, committed_day: date
) -> None:
    """AC-19's persistence half: the wrapper's commit is what ends transaction A before the call, so
    the figure is durable while the request is still 'in the model'."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        ledger = CommittingSpendLedger(SqlSpendLedger(session), session)
        await ledger.try_reserve(committed_day, MicroUsd(250), MicroUsd(1000))

        assert await _committed_spent(engine, committed_day) == 250

        await ledger.settle(committed_day, MicroUsd(250), MicroUsd(90))

    assert await _committed_spent(engine, committed_day) == 90
