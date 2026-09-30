"""The authorization matrix, at use-case level (slice 2.3, T10 RED, AC-7, AC-8, H-9, H-14, H-31,
H-36).

**Authorization is one equality, everywhere**: `row.owner == resolved requester`. Four principals —
guest A, guest B, user A, user B — each own one row, and each asks for every row. On the diagonal
the owner succeeds; everywhere else the context's public `…NotFound` is raised, chained on
`__cause__` from `…NotOwnedBySession` when a **guest** asked and `…NotOwnedByUser` when a **user**
asked. A user-owned row on a guest's request is "not mine" exactly as another session's row is — and
the reverse. Never a distinguishable "not yours": the public type is the one a nonexistent id raises.

Nine use cases share the matrix: `GetBaseCv`, `GetJobPosting`, `GetTailoringRun`,
`ReviseTailoredDocument`, `RequestExport`, `GetExportJob`, `DownloadExportFile`, `ListExportsForRun`,
`RenderDocumentInline`. The last five reach their row through a composed read use case, so their
"not mine" is that use case's `…NotFound` — which is itself the claim: the composition carries the
rule to every caller.

AC-7's "resolve before any read" is proven by a row **owned by an erased user**: an implementation
that skipped `resolve_existing_user` would compare `row.owner == requester`, find them equal, and
hand the row over. The resolution is the only thing that can refuse it, so `UserNotFound` — exactly
that type — is a discriminating assertion (H-9).

Types are asserted exactly (`type(exc) is X`): `NotImplementedError` subclasses `RuntimeError`.

**Green on arrival, and why that is legitimate:** every cell whose *requester* is a guest (the guest
arms already compare `row.owner != owner`, so a user-owned row is refused through the same equality)
and the expired/unknown-session rows — regression guards for the generalization. Every cell whose
requester is a user is red on the user arm's `NotImplementedError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.download_export_file import DownloadExportFile
from tailorcraft.application.export.get_export_job import ExportJobLookup, GetExportJob
from tailorcraft.application.export.list_exports_for_run import ExportListing, ListExportsForRun
from tailorcraft.application.export.render_document_inline import (
    RenderDocumentInline,
    RenderDocumentInlineCommand,
    RenderedInlineDocument,
)
from tailorcraft.application.export.request_export import (
    RequestExport,
    RequestExportCommand,
    RequestExportResult,
)
from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.application.tailoring.revise_tailored_document import (
    ReviseCvCommand,
    ReviseTailoredDocument,
)
from tailorcraft.domain.export.errors import (
    ExportJobNotFound,
    ExportJobNotOwnedBySession,
    ExportJobNotOwnedByUser,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.errors import (
    GuestSessionExpired,
    GuestSessionNotFound,
    UserNotFound,
)
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import (
    BaseCvNotFound,
    BaseCvNotOwnedBySession,
    BaseCvNotOwnedByUser,
)
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.errors import (
    JobPostingNotFound,
    JobPostingNotOwnedBySession,
    JobPostingNotOwnedByUser,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.errors import (
    TailoringRunNotFound,
    TailoringRunNotOwnedBySession,
    TailoringRunNotOwnedByUser,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoredCv,
    TailoredDocumentKind,
    TailoringRunId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeDocumentRenderer,
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import (
    extracted_cv,
    pasted_posting,
    ready_export,
    seed_expired_session,
    seed_user,
    succeeded_run,
)

_EXPORT_BYTES = b"%PDF-1.7 export bytes"
_INLINE_BYTES = b"# Tailored CV\n"
_REVISED = TailoredCv("r" * 500)

PRINCIPALS = ("guest_a", "guest_b", "user_a", "user_b")


@dataclass
class World:
    clock: FixedClock
    sessions: FakeGuestSessionRepository
    users: FakeUserRepository
    cvs: FakeBaseCvRepository
    postings: FakeJobPostingRepository
    runs: FakeTailoringRunRepository
    jobs: FakeExportJobRepository
    files: InMemoryFileStore
    renderer: FakeDocumentRenderer
    events: RecordingEventPublisher
    principals: dict[str, Owner]

    def get_run(self) -> GetTailoringRun:
        return GetTailoringRun(self.runs, self.sessions, self.users, self.clock)

    def get_export_job(self) -> GetExportJob:
        return GetExportJob(self.jobs, self.runs, self.sessions, self.users, self.clock)


async def _world(clock: FixedClock) -> World:
    sessions = FakeGuestSessionRepository()
    users = FakeUserRepository()
    guest_a = await create_active_session(sessions, clock, token_hash="guest-a".ljust(64, "0"))
    guest_b = await create_active_session(sessions, clock, token_hash="guest-b".ljust(64, "0"))
    registered_at = clock.now() - timedelta(days=30)
    return World(
        clock=clock,
        sessions=sessions,
        users=users,
        cvs=FakeBaseCvRepository(),
        postings=FakeJobPostingRepository(),
        runs=FakeTailoringRunRepository(),
        jobs=FakeExportJobRepository(),
        files=InMemoryFileStore(),
        renderer=FakeDocumentRenderer(_INLINE_BYTES),
        events=RecordingEventPublisher(),
        principals={
            "guest_a": GuestOwner(guest_a.id),
            "guest_b": GuestOwner(guest_b.id),
            "user_a": await seed_user(users, "a@example.com", registered_at),
            "user_b": await seed_user(users, "b@example.com", registered_at),
        },
    )


# --- Seeds: one row owned by `owner`, returning its id -------------------------------------------


def _earlier(w: World) -> timedelta:
    return timedelta(minutes=10)


async def _seed_cv(w: World, owner: Owner) -> object:
    cv = extracted_cv(owner, w.clock.now() - _earlier(w))
    await w.cvs.add(cv)
    return cv.id


async def _seed_posting(w: World, owner: Owner) -> object:
    posting = pasted_posting(owner, w.clock.now() - _earlier(w))
    await w.postings.add(posting)
    return posting.id


async def _seed_run(w: World, owner: Owner) -> object:
    run = succeeded_run(owner, w.clock.now() - _earlier(w))
    await w.runs.add(run)
    return run.id


async def _seed_export(w: World, owner: Owner) -> object:
    run = succeeded_run(owner, w.clock.now() - _earlier(w))
    await w.runs.add(run)
    job = ready_export(owner, run, w.clock.now() - _earlier(w), byte_size=len(_EXPORT_BYTES))
    await w.jobs.add(job)
    await w.files.put(job.storage_ref, _EXPORT_BYTES)
    return job.id


# --- Calls: the use case under test, asked by `requester` ----------------------------------------


def _cv_id(row_id: object) -> BaseCvId:
    assert isinstance(row_id, BaseCvId)
    return row_id


def _posting_id(row_id: object) -> JobPostingId:
    assert isinstance(row_id, JobPostingId)
    return row_id


def _run_id(row_id: object) -> TailoringRunId:
    assert isinstance(row_id, TailoringRunId)
    return row_id


def _job_id(row_id: object) -> ExportJobId:
    assert isinstance(row_id, ExportJobId)
    return row_id


async def _get_base_cv(w: World, row_id: object, requester: Owner) -> object:
    return await GetBaseCv(w.cvs, w.sessions, w.users, w.clock)(_cv_id(row_id), requester)


async def _get_job_posting(w: World, row_id: object, requester: Owner) -> object:
    use_case = GetJobPosting(w.postings, w.sessions, w.users, w.clock)
    return await use_case(_posting_id(row_id), requester)


async def _get_tailoring_run(w: World, row_id: object, requester: Owner) -> object:
    return await w.get_run()(_run_id(row_id), requester)


async def _revise(w: World, row_id: object, requester: Owner) -> object:
    run_id = _run_id(row_id)
    stored = await w.runs.find(run_id)
    expected_version = stored.version if stored is not None else 1
    use_case = ReviseTailoredDocument(w.runs, w.get_run(), w.events, w.clock)
    return await use_case(
        ReviseCvCommand(
            tailoring_run_id=run_id,
            requester=requester,
            content=_REVISED,
            expected_version=expected_version,
        )
    )


async def _request_export(w: World, row_id: object, requester: Owner) -> object:
    use_case = RequestExport(w.jobs, w.get_run(), w.events, w.clock)
    return await use_case(
        RequestExportCommand(
            requester=requester,
            tailoring_run_id=_run_id(row_id),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
        )
    )


async def _get_export_job(w: World, row_id: object, requester: Owner) -> object:
    return await w.get_export_job()(_job_id(row_id), requester)


async def _download(w: World, row_id: object, requester: Owner) -> object:
    return await DownloadExportFile(w.get_export_job(), w.files)(_job_id(row_id), requester)


async def _list_exports(w: World, row_id: object, requester: Owner) -> object:
    return await ListExportsForRun(w.jobs, w.get_run())(_run_id(row_id), requester)


async def _render_inline(w: World, row_id: object, requester: Owner) -> object:
    use_case = RenderDocumentInline(w.get_run(), w.renderer, w.events, w.clock)
    return await use_case(
        RenderDocumentInlineCommand(
            requester=requester,
            tailoring_run_id=_run_id(row_id),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.MD,
        )
    )


# --- Success: what the owner gets back -----------------------------------------------------------


async def _owns_cv(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, BaseCv)
    assert result.owner == owner


async def _owns_posting(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, JobPosting)
    assert result.owner == owner


async def _owns_run(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, TailoringRun)
    assert result.owner == owner


async def _revised(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, TailoringRun)
    assert result.owner == owner
    assert result.cv_edited_at == w.clock.now()


async def _export_requested(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, RequestExportResult)
    assert result.created is True
    assert result.export_job.owner == owner


async def _export_job_found(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, ExportJobLookup)
    assert result.job.owner == owner


async def _downloaded(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, tuple)
    job, data = result
    assert isinstance(job, ExportJob)
    assert job.owner == owner
    assert data == _EXPORT_BYTES


async def _listed(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, ExportListing)
    run = next(r for r in w.runs.all() if r.owner == owner)
    assert result.run_version == run.version


async def _rendered(w: World, result: object, owner: Owner) -> None:
    assert isinstance(result, RenderedInlineDocument)
    assert result.data == _INLINE_BYTES


@dataclass(frozen=True)
class Case:
    name: str
    seed: Callable[[World, Owner], Awaitable[object]]
    call: Callable[[World, object, Owner], Awaitable[object]]
    succeeded: Callable[[World, object, Owner], Awaitable[None]]
    missing_id: Callable[[], object]
    not_found: type[Exception]
    guest_cause: type[Exception]
    user_cause: type[Exception]


_RUN_ERRORS = (TailoringRunNotFound, TailoringRunNotOwnedBySession, TailoringRunNotOwnedByUser)
_JOB_ERRORS = (ExportJobNotFound, ExportJobNotOwnedBySession, ExportJobNotOwnedByUser)

CASES = [
    Case(
        "GetBaseCv",
        _seed_cv,
        _get_base_cv,
        _owns_cv,
        lambda: BaseCvId(value=uuid4()),
        BaseCvNotFound,
        BaseCvNotOwnedBySession,
        BaseCvNotOwnedByUser,
    ),
    Case(
        "GetJobPosting",
        _seed_posting,
        _get_job_posting,
        _owns_posting,
        lambda: JobPostingId(value=uuid4()),
        JobPostingNotFound,
        JobPostingNotOwnedBySession,
        JobPostingNotOwnedByUser,
    ),
    Case(
        "GetTailoringRun",
        _seed_run,
        _get_tailoring_run,
        _owns_run,
        lambda: TailoringRunId(value=uuid4()),
        *_RUN_ERRORS,
    ),
    Case(
        "ReviseTailoredDocument",
        _seed_run,
        _revise,
        _revised,
        lambda: TailoringRunId(value=uuid4()),
        *_RUN_ERRORS,
    ),
    Case(
        "RequestExport",
        _seed_run,
        _request_export,
        _export_requested,
        lambda: TailoringRunId(value=uuid4()),
        *_RUN_ERRORS,
    ),
    Case(
        "GetExportJob",
        _seed_export,
        _get_export_job,
        _export_job_found,
        lambda: ExportJobId(value=uuid4()),
        *_JOB_ERRORS,
    ),
    Case(
        "DownloadExportFile",
        _seed_export,
        _download,
        _downloaded,
        lambda: ExportJobId(value=uuid4()),
        *_JOB_ERRORS,
    ),
    Case(
        "ListExportsForRun",
        _seed_run,
        _list_exports,
        _listed,
        lambda: TailoringRunId(value=uuid4()),
        *_RUN_ERRORS,
    ),
    Case(
        "RenderDocumentInline",
        _seed_run,
        _render_inline,
        _rendered,
        lambda: TailoringRunId(value=uuid4()),
        *_RUN_ERRORS,
    ),
]

_CASE_PARAMS = [pytest.param(case, id=case.name) for case in CASES]
_OFF_DIAGONAL = [
    pytest.param(requester, row_owner, id=f"{requester}-asks-for-{row_owner}")
    for requester in PRINCIPALS
    for row_owner in PRINCIPALS
    if requester != row_owner
]


@pytest.mark.parametrize("case", _CASE_PARAMS)
@pytest.mark.parametrize("principal", PRINCIPALS)
async def test_the_owner_reaches_its_own_row(case: Case, principal: str, clock: FixedClock) -> None:
    world = await _world(clock)
    owner = world.principals[principal]
    row_id = await case.seed(world, owner)

    result = await case.call(world, row_id, owner)

    await case.succeeded(world, result, owner)


@pytest.mark.parametrize("case", _CASE_PARAMS)
@pytest.mark.parametrize(("requester", "row_owner"), _OFF_DIAGONAL)
async def test_anyone_else_gets_not_found_chained_from_their_own_kind_of_not_owned(
    case: Case, requester: str, row_owner: str, clock: FixedClock
) -> None:
    world = await _world(clock)
    row_id = await case.seed(world, world.principals[row_owner])
    asking = world.principals[requester]

    with pytest.raises(case.not_found) as exc_info:
        await case.call(world, row_id, asking)

    assert type(exc_info.value) is case.not_found
    expected_cause = case.guest_cause if isinstance(asking, GuestOwner) else case.user_cause
    assert type(exc_info.value.__cause__) is expected_cause


@pytest.mark.parametrize("case", _CASE_PARAMS)
@pytest.mark.parametrize("requester", PRINCIPALS)
async def test_a_nonexistent_id_gets_the_same_not_found_with_no_ownership_cause(
    case: Case, requester: str, clock: FixedClock
) -> None:
    """The column that makes the off-diagonal cells meaningful: the same public type, and a
    `__cause__` that is neither ownership reason — so "not yours" and "not there" are one answer
    outside the use case and two inside it."""
    world = await _world(clock)
    asking = world.principals[requester]

    with pytest.raises(case.not_found) as exc_info:
        await case.call(world, case.missing_id(), asking)

    assert type(exc_info.value) is case.not_found
    assert type(exc_info.value.__cause__) not in (case.guest_cause, case.user_cause)


@pytest.mark.parametrize("case", _CASE_PARAMS)
async def test_an_erased_user_is_refused_before_its_own_row_is_read(
    case: Case, clock: FixedClock
) -> None:
    """AC-7 / H-9: the row is owned by `UserOwner(u)` and `u` asks for it — but `u`'s account is
    gone. Without the resolution the equality would hold and the row would be handed over; only
    `resolve_owner` can refuse, so `UserNotFound` (and not the context's `…NotFound`) proves it ran
    first."""
    world = await _world(clock)
    erased = UserOwner(UserId(value=uuid4()))  # never registered, or since erased
    row_id = await case.seed(world, erased)

    with pytest.raises(UserNotFound) as exc_info:
        await case.call(world, row_id, erased)

    assert type(exc_info.value) is UserNotFound


@pytest.mark.parametrize("case", _CASE_PARAMS)
async def test_an_expired_guest_session_is_refused_before_its_own_row_is_read(
    case: Case, clock: FixedClock
) -> None:
    """AC-7's guest arm, the same shape: the expired session owns the row, and is still refused."""
    world = await _world(clock)
    expired = await seed_expired_session(world.sessions, "expired", clock.now())
    owner = GuestOwner(expired.id)
    row_id = await case.seed(world, owner)

    with pytest.raises(GuestSessionExpired) as exc_info:
        await case.call(world, row_id, owner)

    assert type(exc_info.value) is GuestSessionExpired


@pytest.mark.parametrize("case", _CASE_PARAMS)
async def test_an_unknown_guest_session_is_refused_before_its_own_row_is_read(
    case: Case, clock: FixedClock
) -> None:
    world = await _world(clock)
    unknown = GuestOwner(GuestSessionId(value=uuid4()))
    row_id = await case.seed(world, unknown)

    with pytest.raises(GuestSessionNotFound) as exc_info:
        await case.call(world, row_id, unknown)

    assert type(exc_info.value) is GuestSessionNotFound
