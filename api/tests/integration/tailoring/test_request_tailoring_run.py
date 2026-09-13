"""Application tests for `RequestTailoringRun` (T9, RED).

**Why these fakes, not a real Postgres:** the same reason `test_upload_base_cv.py`'s and
`test_capture_job_posting.py`'s module docstrings give — `infrastructure/persistence/mapping/tailoring/`
has no mapping module yet and there is no migration bringing `tailorcraft_test` to head, so a test
importing `conftest.py`'s `session`/`engine` fixtures would fail for a reason that has nothing to do
with `RequestTailoringRun`. Writing the red honestly means testing the use case against the ports it
actually depends on: in-memory fakes of `TailoringRunRepository`, `BaseCvRepository`,
`JobPostingRepository` and `GuestSessionRepository`, imported from `tests/integration/fakes.py`
(shared, per that module's docstring, so a second hand-written copy never has the chance to drift).

The aggregates are not faked — `BaseCv`, `JobPosting`, `GuestSession` and `TailoringRun` are the real
domain classes, and so are the two composed use cases (`GetBaseCvForSession`,
`GetJobPostingForSession`) `RequestTailoringRun` is built from — the whole point of composing them,
per the use case's own docstring, is that the ownership rule they carry is exercised for real here
rather than assumed.

Every assertion below states what `RequestTailoringRun.__call__` **should** do per
technical-plan.md's "Flow" section (~lines 407-457) and feature-spec.md's failure contract rows
G-6 … G-10, never what the (currently `NotImplementedError`) code was observed doing.

**The assertion that matters most across this whole file** is AC/OQ-2: nothing is added to the
repository on *any* rejection path. Every raise the flow can produce happens before step 6
(`runs.add(...)`), so every rejecting test below asserts `runs.all()` is unchanged from before the
call — not merely that the call raised. A use case that "helpfully" recorded a rejected attempt
would still raise the right exception type and would still fail exactly one of these assertions,
which is the point of writing it into every one of them rather than once.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.get_base_cv import GetBaseCvForSession
from tailorcraft.application.posting.get_job_posting import GetJobPostingForSession
from tailorcraft.application.tailoring.request_tailoring_run import (
    RequestTailoringRun,
    RequestTailoringRunCommand,
    RequestTailoringRunResult,
)
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedBySession
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.posting.errors import JobPostingNotFound, JobPostingNotOwnedBySession
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.errors import (
    BaseCvNotReadyForTailoring,
    TailoringAlreadyRunning,
    TooManyTailoringRuns,
)
from tailorcraft.domain.tailoring.events import TailoringRunRequested
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------

# A single, well-formed `FileRef` key (the grammar in `domain/shared/files.py`) reused by every
# `BaseCv` this module builds — its content is irrelevant to every test here, only its existence.
_A_FILE_REF = FileRef(key="01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf")


def _extracted_text(marker: str = "x") -> ExtractedText:
    """220 non-whitespace characters — comfortably past `ExtractedText`'s 200-character floor."""
    return ExtractedText(marker * 220)


def _posting_text(marker: str = "p") -> JobPostingText:
    """150 non-whitespace characters — comfortably past `JobPostingText`'s 100-character floor and
    under its 30,000-character ceiling."""
    return JobPostingText(marker * 150)


def _uploaded_base_cv(session_id: GuestSessionId, *, at: object) -> BaseCv:
    """A `BaseCv` still in `UPLOADED` — one of G-8's two non-extracted statuses."""
    return BaseCv.upload(
        BaseCvId(value=uuid4()),
        session_id,
        OriginalFilename("cv.pdf"),
        CvContentType.PDF,
        1024,
        _A_FILE_REF,
        at,  # type: ignore[arg-type]
    )


def _extraction_failed_base_cv(session_id: GuestSessionId, *, at: object) -> BaseCv:
    """A `BaseCv` in `EXTRACTION_FAILED` — G-8's other non-extracted status."""
    cv = _uploaded_base_cv(session_id, at=at)
    cv.mark_extraction_failed(ExtractionFailureReason.EXTRACTOR_ERROR, at)  # type: ignore[arg-type]
    return cv


def _extracted_base_cv(session_id: GuestSessionId, *, at: object) -> BaseCv:
    """A `BaseCv` in `EXTRACTED` — the only status `RequestTailoringRun` accepts."""
    cv = _uploaded_base_cv(session_id, at=at)
    cv.mark_extracted(_extracted_text(), at)  # type: ignore[arg-type]
    return cv


def _job_posting(session_id: GuestSessionId, *, at: object) -> JobPosting:
    return JobPosting.from_pasted_text(
        id=JobPostingId(value=uuid4()),
        guest_session_id=session_id,
        text=_posting_text(),
        created_at=at,  # type: ignore[arg-type]
    )


def _queued_run(session_id: GuestSessionId, *, at: object) -> TailoringRun:
    return TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=at,  # type: ignore[arg-type]
    )


def _running_run(session_id: GuestSessionId, *, at: object) -> TailoringRun:
    run = _queued_run(session_id, at=at)
    run.mark_started(at)  # type: ignore[arg-type]
    return run


def _terminal_run(session_id: GuestSessionId, *, at: object) -> TailoringRun:
    """A `TailoringRun` walked all the way to `succeeded` — not active, so it never trips G-9's
    check, which is what lets it stand in for "a run this session already owns" in the cap test
    without also looking like a run in flight."""
    run = _queued_run(session_id, at=at)
    run.mark_started(at)  # type: ignore[arg-type]
    run.mark_succeeded(
        TailoredDocuments(cv=TailoredCv("x" * 500), cover_letter=CoverLetter("y" * 300)),
        LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
        ),
        at,  # type: ignore[arg-type]
    )
    return run


def _use_case(
    runs: FakeTailoringRunRepository,
    cvs: FakeBaseCvRepository,
    postings: FakeJobPostingRepository,
    sessions: FakeGuestSessionRepository,
    events: RecordingEventPublisher,
    clock: FixedClock,
) -> RequestTailoringRun:
    """Builds `RequestTailoringRun` from the two *composed use cases* its own docstring insists on
    — `GetBaseCvForSession` and `GetJobPostingForSession`, real, not faked — so that every test in
    this module exercises the actual ownership check rather than a stand-in for it. Never passes
    `max_per_session`: only the cap test (`test_twentieth_run_succeeds_...`) cares about that
    parameter, and it constructs its own instance with the default left implicit, on purpose (see
    that test's docstring)."""
    return RequestTailoringRun(
        runs,
        GetBaseCvForSession(cvs, sessions, clock),
        GetJobPostingForSession(postings, sessions, clock),
        events,
        clock,
    )


# --- 1. Happy path ---------------------------------------------------------------------------------


async def test_happy_path_returns_queued_result_saves_and_publishes_after_the_save(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session.id, at=clock.now())
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session.id, at=clock.now())
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher(repo=runs)
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=session.id, base_cv_id=cv.id, job_posting_id=posting.id
    )
    result = await use_case(cmd)

    assert isinstance(result, RequestTailoringRunResult)
    assert result.status is TailoringRunStatus.QUEUED
    assert result.requested_at == clock.now()

    stored = await runs.get(result.tailoring_run_id)
    assert stored.guest_session_id == session.id
    assert stored.base_cv_id == cv.id
    assert stored.job_posting_id == posting.id
    assert stored.status is TailoringRunStatus.QUEUED

    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, TailoringRunRequested)
    assert event.tailoring_run_id == result.tailoring_run_id
    assert event.guest_session_id == session.id
    assert event.base_cv_id == cv.id
    assert event.job_posting_id == posting.id
    # positive evidence of publish-after-save ordering, not just an end-state check
    assert events.repo_size_at_first_publish == 1


# --- 2. G-6: base_cv_id owned by a different session -------------------------------------------------


async def test_base_cv_owned_by_a_different_session_raises_base_cv_not_found_chained_from_not_owned(
    clock: FixedClock,
) -> None:
    """The 404 collapse (F-20/AC-8, ADR-0008 applied here as G-6): the public exception must be
    indistinguishable from "no such CV", and `__cause__` is asserted rather than only the type,
    because that chain is the only thing proving the ownership check actually ran instead of the id
    simply being absent from the fake."""
    sessions = FakeGuestSessionRepository()
    mine = await create_active_session(sessions, clock, token_hash="mine".ljust(64, "0"))
    other = await create_active_session(sessions, clock, token_hash="other".ljust(64, "0"))
    cvs = FakeBaseCvRepository()
    someone_elses_cv = _extracted_base_cv(other.id, at=clock.now())
    await cvs.add(someone_elses_cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(mine.id, at=clock.now())
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=mine.id, base_cv_id=someone_elses_cv.id, job_posting_id=posting.id
    )

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(cmd)

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedBySession)
    assert runs.all() == []
    assert events.published == []


# --- 3. G-7: job_posting_id owned by a different session ---------------------------------------------


async def test_job_posting_owned_by_a_different_session_raises_job_posting_not_found_chained(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    mine = await create_active_session(sessions, clock, token_hash="mine".ljust(64, "0"))
    other = await create_active_session(sessions, clock, token_hash="other".ljust(64, "0"))
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(mine.id, at=clock.now())
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    someone_elses_posting = _job_posting(other.id, at=clock.now())
    await postings.add(someone_elses_posting)
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=mine.id, base_cv_id=cv.id, job_posting_id=someone_elses_posting.id
    )

    with pytest.raises(JobPostingNotFound) as exc_info:
        await use_case(cmd)

    assert isinstance(exc_info.value.__cause__, JobPostingNotOwnedBySession)
    assert runs.all() == []
    assert events.published == []


# --- 4. G-8: base CV not yet extracted, both non-extracted statuses -----------------------------------

_NOT_READY_CV_BUILDERS = [
    pytest.param(_uploaded_base_cv, id="G-8-uploaded"),
    pytest.param(_extraction_failed_base_cv, id="G-8-extraction_failed"),
]


@pytest.mark.parametrize("build_cv", _NOT_READY_CV_BUILDERS)
async def test_base_cv_not_extracted_raises_base_cv_not_ready_for_tailoring(
    clock: FixedClock, build_cv: object
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    cv = build_cv(session.id, at=clock.now())  # type: ignore[operator]
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session.id, at=clock.now())
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=session.id, base_cv_id=cv.id, job_posting_id=posting.id
    )

    with pytest.raises(BaseCvNotReadyForTailoring):
        await use_case(cmd)

    assert runs.all() == []
    assert events.published == []


# --- 5. G-9: an active run already exists, queued or running -----------------------------------------

_ACTIVE_RUN_BUILDERS = [
    pytest.param(_queued_run, id="G-9-queued"),
    pytest.param(_running_run, id="G-9-running"),
]


@pytest.mark.parametrize("build_active_run", _ACTIVE_RUN_BUILDERS)
async def test_active_run_raises_tailoring_already_running_carrying_its_id(
    clock: FixedClock, build_active_run: object
) -> None:
    """G-9. The error must carry the *active* run's id — the whole reason it exists is to hand the
    router something the client can attach a poller to — so the id is asserted, not just the type."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session.id, at=clock.now())
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session.id, at=clock.now())
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    active_run = build_active_run(session.id, at=clock.now())  # type: ignore[operator]
    await runs.add(active_run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=session.id, base_cv_id=cv.id, job_posting_id=posting.id
    )

    with pytest.raises(TailoringAlreadyRunning) as exc_info:
        await use_case(cmd)

    assert exc_info.value.active_run_id == active_run.id
    # no second row: only the pre-existing active run is in the repository
    assert runs.all() == [active_run]
    assert events.published == []


# --- 6. G-10: the cap, boundary hard-coded at 20/21 ---------------------------------------------------


async def test_twentieth_run_succeeds_and_twenty_first_raises_too_many_tailoring_runs(
    clock: FixedClock,
) -> None:
    """Hard-codes 20 and 21 rather than reading `max_per_session` off the use case, and builds
    `RequestTailoringRun` with its default left implicit (`_use_case` never passes the parameter):
    the point of this test is that it *disagrees with the code* — and therefore fails — the moment
    somebody changes the default cap without deciding to update this number and the spec together.
    A test that read the default back off the instance and looped that many times would pass no
    matter what the default became, which would ratify a silent change instead of catching one.

    Nineteen pre-existing runs are built directly in a *terminal* state (`succeeded`) via
    `_terminal_run`, so the G-9 active-run check (step 4, which runs before the G-10 cap check, step
    5) never fires for them — this test is about the cap, not the active-run rule. The twentieth run
    is created through the real `use_case(cmd)` call, proving the boundary is actually accepted
    rather than merely assumed, and is then walked to `succeeded` exactly as a worker eventually
    would, so the twenty-first call below tests the cap instead of colliding with G-9.
    """
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session.id, at=clock.now())
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session.id, at=clock.now())
    await postings.add(posting)
    runs = FakeTailoringRunRepository()

    for _ in range(19):
        await runs.add(_terminal_run(session.id, at=clock.now()))

    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)
    cmd = RequestTailoringRunCommand(
        guest_session_id=session.id, base_cv_id=cv.id, job_posting_id=posting.id
    )

    twentieth = await use_case(cmd)

    assert isinstance(twentieth, RequestTailoringRunResult)
    assert len(runs.all()) == 20

    # Walk the twentieth run to `succeeded`, the way a worker eventually would, so the twenty-first
    # call below is testing the cap rather than tripping G-9's active-run rule instead.
    twentieth_run = await runs.get(twentieth.tailoring_run_id)
    twentieth_run.mark_started(clock.now())
    twentieth_run.mark_succeeded(
        TailoredDocuments(cv=TailoredCv("x" * 500), cover_letter=CoverLetter("y" * 300)),
        LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
        ),
        clock.now(),
    )
    await runs.save(twentieth_run)

    with pytest.raises(TooManyTailoringRuns):
        await use_case(cmd)

    # the cap is checked before a new run is minted — still 20, no twenty-first row
    assert len(runs.all()) == 20


# --- 7. Expired / missing guest session ---------------------------------------------------------------


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-token-hash".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    cvs = FakeBaseCvRepository()
    postings = FakeJobPostingRepository()
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    cmd = RequestTailoringRunCommand(
        guest_session_id=expired.id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
    )

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert runs.all() == []
    assert events.published == []


async def test_missing_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    cvs = FakeBaseCvRepository()
    postings = FakeJobPostingRepository()
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(runs, cvs, postings, sessions, events, clock)

    unknown_session_id = GuestSessionId(value=uuid4())
    cmd = RequestTailoringRunCommand(
        guest_session_id=unknown_session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
    )

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert runs.all() == []
    assert events.published == []
