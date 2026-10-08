"""AC-5 and AC-6 against the real `SqlAlchemyExportJobRepository` (3.2, T12; test-after).

The in-memory fake proves the use case; this proves the SQL adapter honours the widened key
(`layout_template IS NOT DISTINCT FROM`) and round-trips an explicit layout. Real Postgres,
rolled back per test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.request_export import RequestExport, RequestExportCommand
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.value_objects import (
    DEFAULT_LAYOUT_TEMPLATE,
    ExportFailureReason,
    ExportFormat,
    ExportJobStatus,
    LayoutTemplate,
)
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tests.integration.export.support import succeeded_run
from tests.integration.fakes import (
    FakeTailoringRunRepository,
    FakeUserRepository,
    RecordingEventPublisher,
)


class _Rig:
    def __init__(self, session: AsyncSession, clock: FixedClock) -> None:
        self.clock = clock
        self.jobs = SqlAlchemyExportJobRepository(session)
        self.runs = FakeTailoringRunRepository()
        self.session = session

    async def start(self) -> tuple[GuestOwner, TailoringRun]:
        sessions = SqlAlchemyGuestSessionRepository(self.session)
        guest = GuestSession.start(
            id=sessions.next_identity(), token_hash="a" * 64, at=self.clock.now(), ttl_hours=24
        )
        await sessions.add(guest)
        run = succeeded_run(session_id=guest.id)
        await self.runs.add(run)
        return GuestOwner(guest.id), run

    def use_case(self) -> RequestExport:
        return RequestExport(
            self.jobs,
            GetTailoringRun(
                self.runs,
                SqlAlchemyGuestSessionRepository(self.session),
                FakeUserRepository(),
                self.clock,
            ),
            RecordingEventPublisher(),
            self.clock,
        )


def _cmd(
    owner: GuestOwner, run: TailoringRun, layout: LayoutTemplate | None
) -> RequestExportCommand:
    return RequestExportCommand(
        requester=owner,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        layout_template=layout,
    )


async def _job_count(rig: _Rig, run: TailoringRun) -> int:
    result = await rig.session.execute(
        select(func.count())
        .select_from(export_job_table)
        .where(export_job_table.c.tailoring_run_id == run.id)
    )
    return int(result.scalar_one())


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_sql_an_explicit_layout_survives_a_reload(
    session: AsyncSession, clock: FixedClock, layout: LayoutTemplate
) -> None:
    rig = _Rig(session, clock)
    owner, run = await rig.start()

    created = await rig.use_case()(_cmd(owner, run, layout))
    session.expunge_all()
    reloaded = await rig.jobs.get(created.export_job.id)

    assert reloaded.layout_template is layout


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_sql_the_same_layout_twice_returns_the_existing_job(
    session: AsyncSession, clock: FixedClock, layout: LayoutTemplate
) -> None:
    rig = _Rig(session, clock)
    owner, run = await rig.start()

    first = await rig.use_case()(_cmd(owner, run, layout))
    second = await rig.use_case()(_cmd(owner, run, layout))

    assert first.created is True
    assert second.created is False
    assert second.export_job.id == first.export_job.id
    assert await _job_count(rig, run) == 1


async def test_sql_a_different_layout_at_the_same_version_is_a_new_job(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    owner, run = await rig.start()

    modern = await rig.use_case()(_cmd(owner, run, LayoutTemplate.MODERN))
    formal = await rig.use_case()(_cmd(owner, run, LayoutTemplate.FORMAL))

    assert modern.created is True
    assert formal.created is True
    assert formal.export_job.id != modern.export_job.id
    assert await _job_count(rig, run) == 2


async def test_sql_an_omitted_layout_matches_an_explicit_default(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    owner, run = await rig.start()

    first = await rig.use_case()(_cmd(owner, run, None))
    second = await rig.use_case()(_cmd(owner, run, DEFAULT_LAYOUT_TEMPLATE))

    assert second.created is False
    assert second.export_job.id == first.export_job.id


async def test_sql_a_failed_job_of_that_layout_does_not_count_as_the_existing_one(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    owner, run = await rig.start()
    failed = (await rig.use_case()(_cmd(owner, run, LayoutTemplate.MODERN))).export_job
    failed.mark_failed(ExportFailureReason.NOT_QUEUED, clock.now())
    failed.release_events()
    await rig.jobs.save(failed)

    result = await rig.use_case()(_cmd(owner, run, LayoutTemplate.MODERN))

    assert result.created is True
    assert result.export_job.id != failed.id
    assert result.export_job.status is ExportJobStatus.QUEUED
