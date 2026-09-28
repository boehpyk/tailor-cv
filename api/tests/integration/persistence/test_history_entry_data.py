"""Persistence tests for `SqlAlchemyHistoryEntryData.delete_history_entry` (slice 2.3, T17, written
after; plan §0.6, AC-14's adapter half, H-44).

Three `DELETE`s in one transaction: the run (only the user's; `RETURNING job_posting_id`, zero rows
→ `None` and nothing else touched), its export jobs (`RETURNING id, format`, each key **derived** by
`FileRef.for_export` — so a `rendering` or `failed` job's bytes are unlinked too), and the posting
when **no remaining run of anyone** references it. Instances the session had already loaded are
expunged, so a later flush cannot target a row that is gone.
"""

from __future__ import annotations

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
)
from tests.integration.owners import pasted_posting, queued_export, ready_export, succeeded_run
from tests.integration.persistence.owner_rows import persist_guest, persist_user


async def _exists(session: AsyncSession, table: Table, row_id: object) -> bool:
    found = await session.execute(select(table.c.id).where(table.c.id == row_id))
    return found.scalar_one_or_none() is not None


async def _entry(
    session: AsyncSession, clock: FixedClock, owner: Owner
) -> tuple[TailoringRun, list[ExportJob]]:
    """A posting, a succeeded run over it, and one job in each state a key can exist in: `ready`
    (file named), `rendering` (bytes possibly written, no key recorded) and `failed` (the same)."""
    posting = pasted_posting(owner, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(owner, clock.now(), job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    ready = ready_export(owner, run, clock.now(), format=ExportFormat.PDF)
    rendering = queued_export(owner, run, clock.now(), format=ExportFormat.DOCX)
    rendering.mark_started(clock.now())
    failed = queued_export(owner, run, clock.now(), format=ExportFormat.PDF)
    failed.mark_started(clock.now())
    failed.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    jobs = SqlAlchemyExportJobRepository(session)
    for job in (ready, rendering, failed):
        await jobs.add(job)
    await session.flush()
    return run, [ready, rendering, failed]


async def test_the_returned_set_is_exactly_the_rows_that_went_with_derived_keys(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run, jobs = await _entry(session, clock, user)

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, run.id.value
    )

    assert deleted is not None
    assert deleted.export_jobs == 3
    assert deleted.posting_deleted is True
    assert set(deleted.export_files) == {FileRef.for_export(job.id, job.format) for job in jobs}
    assert len(deleted.export_files) == 3
    assert not await _exists(session, tailoring_run_table, run.id)
    assert not await _exists(session, job_posting_table, run.job_posting_id)
    for job in jobs:
        assert not await _exists(session, export_job_table, job.id)


async def test_a_posting_another_users_run_still_references_is_kept(
    session: AsyncSession, clock: FixedClock
) -> None:
    """ "No remaining run of anyone": the second reference is another owner's run (a dangling
    cross-owner id — unreachable through the use cases, which is why the rule does not care)."""
    user = await persist_user(session, clock)
    run, _ = await _entry(session, clock, user)
    other = succeeded_run(
        await persist_guest(session, clock), clock.now(), job_posting_id=run.job_posting_id
    )
    await SqlAlchemyTailoringRunRepository(session).add(other)
    await session.flush()

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, run.id.value
    )

    assert deleted is not None
    assert deleted.posting_deleted is False
    assert await _exists(session, job_posting_table, run.job_posting_id)
    assert not await _exists(session, tailoring_run_table, run.id)
    assert await _exists(session, tailoring_run_table, other.id)


async def test_a_posting_the_users_other_run_references_is_kept(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run, _ = await _entry(session, clock, user)
    sibling = succeeded_run(user, clock.now(), job_posting_id=run.job_posting_id)
    await SqlAlchemyTailoringRunRepository(session).add(sibling)
    await session.flush()

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, run.id.value
    )

    assert deleted is not None
    assert deleted.export_jobs == 3
    assert deleted.posting_deleted is False
    assert await _exists(session, job_posting_table, run.job_posting_id)


async def test_another_users_run_is_none_and_nothing_is_touched(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    intruder = await persist_user(session, clock)
    run, jobs = await _entry(session, clock, owner)

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        intruder.user_id, run.id.value
    )

    assert deleted is None
    assert await _exists(session, tailoring_run_table, run.id)
    assert await _exists(session, job_posting_table, run.job_posting_id)
    for job in jobs:
        assert await _exists(session, export_job_table, job.id)


async def test_the_loser_of_a_second_deletion_gets_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run, _ = await _entry(session, clock, user)
    data = SqlAlchemyHistoryEntryData(session)
    first = await data.delete_history_entry(user.user_id, run.id.value)

    second = await data.delete_history_entry(user.user_id, run.id.value)

    assert first is not None
    assert second is None


async def test_already_loaded_instances_are_expunged_so_a_later_flush_is_harmless(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The use case loaded the run through `GetTailoringRun`; a Core `DELETE` does not tell the ORM.
    After the deletion the loaded run, job and posting are no longer in the session, a flush has
    nothing stale to write, and a fresh read finds nothing."""
    user = await persist_user(session, clock)
    run, jobs = await _entry(session, clock, user)
    session.expunge_all()
    runs = SqlAlchemyTailoringRunRepository(session)
    loaded_run = await runs.get(run.id)
    loaded_job = await SqlAlchemyExportJobRepository(session).get(jobs[0].id)
    loaded_posting = await SqlAlchemyJobPostingRepository(session).get(run.job_posting_id)

    await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run.id.value)

    assert loaded_run not in session
    assert loaded_job not in session
    assert loaded_posting not in session
    await session.flush()
    assert await runs.find(run.id) is None
