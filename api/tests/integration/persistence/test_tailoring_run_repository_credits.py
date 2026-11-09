"""`SqlAlchemyTailoringRunRepository`'s two slice-5.2 reads (T26, test-after): `find_active_for_owner`
with `excluding`, the charge's hard re-check (plan §0.3), and `list_lost_queued`, the sweep's second
list (plan §0.5). The concurrent half of the re-check is T14's `concurrent_app` test.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tests.integration.persistence.test_tailoring_run_repository import (
    _persist_owner,
    _queued,
    _running,
)


async def test_find_active_for_owner_excluding_a_run_answers_another_or_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="c1" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    mine = await _queued(session, runs, owner.id, clock)
    await runs.add(mine)

    assert await runs.find_active_for_owner(GuestOwner(owner.id), excluding=mine.id) is None

    clock.advance(1)
    other = await _queued(session, runs, owner.id, clock)
    await runs.add(other)

    found = await runs.find_active_for_owner(GuestOwner(owner.id), excluding=mine.id)
    assert found is not None
    assert found.id == other.id


async def test_list_lost_queued_lists_queued_runs_requested_strictly_before_oldest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="c2" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    older = await _queued(session, runs, owner.id, clock)
    still_running = await _running(session, runs, owner.id, clock)
    clock.advance(1)
    newer = await _queued(session, runs, owner.id, clock)
    clock.advance(1)
    at_the_cutoff = await _queued(session, runs, owner.id, clock)
    for run in (older, still_running, newer, at_the_cutoff):
        await runs.add(run)

    listed = [run.id for run in await runs.list_lost_queued(clock.now(), limit=1000)]

    assert [i for i in listed if i in {older.id, newer.id}] == [older.id, newer.id]
    assert still_running.id not in listed
    assert at_the_cutoff.id not in listed, "strictly before: the cutoff itself is not lost"
    assert len(await runs.list_lost_queued(clock.now(), limit=1)) == 1
