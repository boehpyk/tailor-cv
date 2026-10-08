"""Application tests for the PDF layout through `RequestExport`, `RenderExportJob` and
`RenderDocumentInline` (slice 3.2, T8 RED; AC-5 ... AC-9).

Written from the acceptance criteria, not from the code. The seam under test: `RequestExport` must
resolve the layout (omitted PDF -> `DEFAULT_LAYOUT_TEMPLATE`, explicit -> as asked, DOCX + layout ->
`LayoutTemplateNotApplicable`) and widen the idempotency key; `RenderExportJob` must hand the
renderer the layout **on the row**; `RenderDocumentInline` must hand it `None`.

**Fakes, not a real Postgres — deliberately, like every other application test in this package.**
`FakeExportJobRepository` honours the layout in `find_latest_for_key` (that is the AC-6 stand-in the
task list names; the SQL adapter ignores the layout until T12). The SQL adapter's own handling of the
key is T12's persistence test, not this file's.
# T12: once the real adapter honours the key, AC-6 additionally runs against it.

**Absence assertions are paired with a positive control** (a skeleton satisfies "no row" trivially).
**Exact types**: `NotImplementedError` subclasses `RuntimeError`, so nothing here uses a base class.

Some tests are green on arrival (they pin behaviour the temporary values already have: the default
for a PDF, `None` for DOCX and inline); they are regression guards for T10 and are marked below.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tailorcraft.application.export.render_document_inline import (
    RenderDocumentInline,
    RenderDocumentInlineCommand,
)
from tailorcraft.application.export.render_export_job import (
    RenderExportJob,
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.application.export.request_export import RequestExport, RequestExportCommand
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.errors import LayoutTemplateNotApplicable, TooManyExportJobs
from tailorcraft.domain.export.events import ExportRequested
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    DEFAULT_LAYOUT_TEMPLATE,
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
    LayoutTemplate,
)
from tailorcraft.domain.identity.ownership import GuestOwner, Owner
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import succeeded_run
from tests.integration.fakes import (
    FakeDocumentRenderer,
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import seed_user
from tests.integration.owners import succeeded_run as owned_succeeded_run

_NON_CLASSIC = [LayoutTemplate.MODERN, LayoutTemplate.FORMAL]


class _World:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.runs = FakeTailoringRunRepository()
        self.jobs = FakeExportJobRepository()
        self.events = RecordingEventPublisher(self.jobs)

    def request_export(self) -> RequestExport:
        return RequestExport(
            self.jobs,
            GetTailoringRun(self.runs, self.sessions, self.users, self.clock),
            self.events,
            self.clock,
        )

    async def guest_run(self) -> tuple[GuestOwner, TailoringRun]:
        session = await create_active_session(self.sessions, self.clock)
        run = succeeded_run(session_id=session.id)
        await self.runs.add(run)
        return GuestOwner(session.id), run

    def seed_job(
        self,
        run: TailoringRun,
        *,
        format: ExportFormat,
        layout_template: LayoutTemplate | None,
        document: TailoredDocumentKind = TailoredDocumentKind.CV,
    ) -> ExportJob:
        job = ExportJob.request(
            id=ExportJobId(value=uuid4()),
            owner=run.owner,
            tailoring_run_id=run.id,
            document=document,
            format=format,
            layout_template=layout_template,
            run_version=run.version,
            requested_at=self.clock.now(),
        )
        job.release_events()
        return job

    def requested_events(self) -> list[ExportRequested]:
        return [e for e in self.events.published if isinstance(e, ExportRequested)]


def _command(
    requester: Owner,
    run: TailoringRun,
    *,
    format: ExportFormat = ExportFormat.PDF,
    layout_template: LayoutTemplate | None = None,
    document: TailoredDocumentKind = TailoredDocumentKind.CV,
) -> RequestExportCommand:
    return RequestExportCommand(
        requester=requester,
        tailoring_run_id=run.id,
        document=document,
        format=format,
        layout_template=layout_template,
    )


# --- AC-5: the layout is resolved and recorded ---------------------------------------------------


async def test_a_pdf_with_no_layout_gets_the_default_on_the_row_and_the_event(
    clock: FixedClock,
) -> None:
    """Green on arrival (the temporary value is Classic); a regression guard for T10."""
    world = _World(clock)
    owner, run = await world.guest_run()

    result = await world.request_export()(_command(owner, run))

    assert result.created is True
    assert result.export_job.layout_template is DEFAULT_LAYOUT_TEMPLATE
    assert world.jobs.all() == [result.export_job]
    assert [e.layout_template for e in world.requested_events()] == [DEFAULT_LAYOUT_TEMPLATE]


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_an_explicit_layout_is_recorded_on_the_row_and_the_event(
    clock: FixedClock, layout: LayoutTemplate
) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()

    result = await world.request_export()(_command(owner, run, layout_template=layout))

    assert result.created is True
    assert result.export_job.layout_template is layout
    assert [job.layout_template for job in world.jobs.all()] == [layout]
    assert [e.layout_template for e in world.requested_events()] == [layout]


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_a_docx_with_a_layout_is_refused_with_no_row_and_no_event(
    clock: FixedClock, layout: LayoutTemplate
) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()
    use_case = world.request_export()

    with pytest.raises(LayoutTemplateNotApplicable):
        await use_case(_command(owner, run, format=ExportFormat.DOCX, layout_template=layout))

    assert world.jobs.all() == []
    assert world.requested_events() == []

    # Positive control: the same request without a layout is accepted, so the absences above are
    # the refusal's doing and not a harness that records nothing.
    ok = await use_case(_command(owner, run, format=ExportFormat.DOCX))
    assert ok.created is True
    assert ok.export_job.layout_template is None
    assert len(world.jobs.all()) == 1
    assert len(world.requested_events()) == 1


# --- AC-6: the key widens ------------------------------------------------------------------------


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_the_same_layout_twice_returns_the_existing_job(
    clock: FixedClock, layout: LayoutTemplate
) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()
    use_case = world.request_export()

    first = await use_case(_command(owner, run, layout_template=layout))
    second = await use_case(_command(owner, run, layout_template=layout))

    assert first.created is True
    assert second.created is False
    assert second.export_job.id == first.export_job.id
    assert len(world.jobs.all()) == 1
    assert len(world.requested_events()) == 1


async def test_a_different_layout_at_the_same_version_is_a_new_job(clock: FixedClock) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()
    use_case = world.request_export()

    modern = await use_case(_command(owner, run, layout_template=LayoutTemplate.MODERN))
    formal = await use_case(_command(owner, run, layout_template=LayoutTemplate.FORMAL))

    assert modern.created is True
    assert formal.created is True
    assert formal.export_job.id != modern.export_job.id
    assert modern.export_job.run_version == formal.export_job.run_version
    assert {job.layout_template for job in world.jobs.all()} == {
        LayoutTemplate.MODERN,
        LayoutTemplate.FORMAL,
    }


async def test_an_omitted_layout_matches_an_explicit_default(clock: FixedClock) -> None:
    """`None` and `DEFAULT_LAYOUT_TEMPLATE` are one key for a PDF, not two."""
    world = _World(clock)
    owner, run = await world.guest_run()
    use_case = world.request_export()

    first = await use_case(_command(owner, run))
    second = await use_case(_command(owner, run, layout_template=DEFAULT_LAYOUT_TEMPLATE))

    assert second.created is False
    assert second.export_job.id == first.export_job.id


async def test_a_failed_job_of_that_layout_does_not_count_as_the_existing_one(
    clock: FixedClock,
) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()
    failed = world.seed_job(run, format=ExportFormat.PDF, layout_template=LayoutTemplate.MODERN)
    failed.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    failed.release_events()
    await world.jobs.add(failed)

    result = await world.request_export()(
        _command(owner, run, layout_template=LayoutTemplate.MODERN)
    )

    assert result.created is True
    assert result.export_job.id != failed.id
    assert result.export_job.layout_template is LayoutTemplate.MODERN
    assert result.export_job.status is ExportJobStatus.QUEUED


async def test_docx_keeps_its_key_with_no_layout_on_either_side(clock: FixedClock) -> None:
    """Green on arrival; a regression guard for T10."""
    world = _World(clock)
    owner, run = await world.guest_run()
    use_case = world.request_export()

    first = await use_case(_command(owner, run, format=ExportFormat.DOCX))
    second = await use_case(_command(owner, run, format=ExportFormat.DOCX))

    assert first.created is True
    assert second.created is False
    assert second.export_job.id == first.export_job.id
    assert first.export_job.layout_template is None


# --- AC-7: the caps count every layout -----------------------------------------------------------


async def test_a_guest_at_forty_jobs_is_refused_whatever_layout_is_asked_for(
    clock: FixedClock,
) -> None:
    """The cap counts every layout. Seeded on the cover letter, in every layout, so no requested
    CV key matches and step 3's idempotent lookup (X-16) cannot answer before the cap does."""
    world = _World(clock)
    owner, run = await world.guest_run()
    layouts = list(LayoutTemplate)
    for i in range(40):
        await world.jobs.add(
            world.seed_job(
                run,
                format=ExportFormat.PDF,
                layout_template=layouts[i % len(layouts)],
                document=TailoredDocumentKind.COVER_LETTER,
            )
        )
    use_case = world.request_export()

    for layout in LayoutTemplate:
        with pytest.raises(TooManyExportJobs):
            await use_case(_command(owner, run, layout_template=layout))
    assert len(world.jobs.all()) == 40


async def test_a_guest_at_forty_jobs_gets_the_existing_job_back_on_a_repeat_request(
    clock: FixedClock,
) -> None:
    world = _World(clock)
    owner, run = await world.guest_run()
    existing = world.seed_job(run, format=ExportFormat.PDF, layout_template=LayoutTemplate.MODERN)
    await world.jobs.add(existing)
    for _ in range(39):
        await world.jobs.add(
            world.seed_job(
                run,
                format=ExportFormat.DOCX,
                layout_template=None,
                document=TailoredDocumentKind.COVER_LETTER,
            )
        )
    assert len(world.jobs.all()) == 40

    result = await world.request_export()(
        _command(owner, run, layout_template=LayoutTemplate.MODERN)
    )

    assert result.created is False
    assert result.export_job.id == existing.id
    assert len(world.jobs.all()) == 40


async def test_a_user_at_twenty_jobs_on_a_run_is_refused_whatever_layout_is_asked_for(
    clock: FixedClock,
) -> None:
    world = _World(clock)
    earlier = clock.now()
    user = await seed_user(world.users, "layouts@example.com", earlier)
    run = owned_succeeded_run(user, earlier)
    await world.runs.add(run)
    layouts = list(LayoutTemplate)
    for i in range(20):  # cover letter, every layout: no requested CV key matches (X-18)
        await world.jobs.add(
            world.seed_job(
                run,
                format=ExportFormat.PDF,
                layout_template=layouts[i % len(layouts)],
                document=TailoredDocumentKind.COVER_LETTER,
            )
        )
    use_case = world.request_export()

    for layout in LayoutTemplate:
        with pytest.raises(TooManyExportJobs):
            await use_case(_command(user, run, layout_template=layout))
    assert len(world.jobs.all()) == 20


async def test_a_user_at_twenty_jobs_gets_the_existing_job_back_on_a_repeat_request(
    clock: FixedClock,
) -> None:
    world = _World(clock)
    earlier = clock.now()
    user = await seed_user(world.users, "repeat@example.com", earlier)
    run = owned_succeeded_run(user, earlier)
    await world.runs.add(run)
    existing = world.seed_job(run, format=ExportFormat.PDF, layout_template=LayoutTemplate.FORMAL)
    await world.jobs.add(existing)
    for _ in range(19):
        await world.jobs.add(
            world.seed_job(
                run,
                format=ExportFormat.DOCX,
                layout_template=None,
                document=TailoredDocumentKind.COVER_LETTER,
            )
        )

    result = await world.request_export()(
        _command(user, run, layout_template=LayoutTemplate.FORMAL)
    )

    assert result.created is False
    assert result.export_job.id == existing.id
    assert len(world.jobs.all()) == 20


# --- AC-8: the worker passes the row's layout, once ----------------------------------------------


def _render_use_case(
    world: _World, renderer: FakeDocumentRenderer, files: InMemoryFileStore
) -> RenderExportJob:
    return RenderExportJob(
        world.jobs, world.runs, renderer, files, world.events, world.clock, stale_after_seconds=300
    )


@pytest.mark.parametrize("layout", _NON_CLASSIC, ids=lambda m: m.value)
async def test_the_worker_hands_the_renderer_the_layout_on_the_row(
    clock: FixedClock, layout: LayoutTemplate
) -> None:
    world = _World(clock)
    _owner, run = await world.guest_run()
    job = world.seed_job(run, format=ExportFormat.PDF, layout_template=layout)
    await world.jobs.add(job)
    renderer = FakeDocumentRenderer(b"%PDF fake")

    outcome = await _render_use_case(world, renderer, InMemoryFileStore())(
        RenderExportJobCommand(export_job_id=job.id)
    )

    assert outcome is RenderExportJobOutcome.READY
    assert len(renderer.calls) == 1
    assert renderer.layout_templates == [layout]


async def test_the_worker_hands_a_docx_render_none(clock: FixedClock) -> None:
    """Green on arrival; a regression guard for T10."""
    world = _World(clock)
    _owner, run = await world.guest_run()
    job = world.seed_job(run, format=ExportFormat.DOCX, layout_template=None)
    await world.jobs.add(job)
    renderer = FakeDocumentRenderer(b"docx fake")

    outcome = await _render_use_case(world, renderer, InMemoryFileStore())(
        RenderExportJobCommand(export_job_id=job.id)
    )

    assert outcome is RenderExportJobOutcome.READY
    assert renderer.layout_templates == [None]


# --- AC-9: inline renders pass None --------------------------------------------------------------


@pytest.mark.parametrize("fmt", [ExportFormat.MD, ExportFormat.TXT], ids=lambda f: f.value)
async def test_an_inline_render_passes_no_layout(clock: FixedClock, fmt: ExportFormat) -> None:
    """Green on arrival; a regression guard for T10."""
    world = _World(clock)
    owner, run = await world.guest_run()
    renderer = FakeDocumentRenderer(b"inline bytes")
    use_case = RenderDocumentInline(
        GetTailoringRun(world.runs, world.sessions, world.users, clock),
        renderer,
        world.events,
        clock,
    )

    await use_case(
        RenderDocumentInlineCommand(
            requester=owner,
            tailoring_run_id=run.id,
            document=TailoredDocumentKind.CV,
            format=fmt,
        )
    )

    assert len(renderer.calls) == 1
    assert renderer.layout_templates == [None]
