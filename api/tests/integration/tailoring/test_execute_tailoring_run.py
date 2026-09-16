"""Application tests for `ExecuteTailoringRun` (T12, RED).

**Why these fakes, not a real Postgres:** the same reason `test_request_tailoring_run.py`'s module
docstring gives — `infrastructure/persistence/mapping/tailoring/` has no mapping module yet and
there is no migration bringing `tailorcraft_test` to head, so a test importing `conftest.py`'s
`session`/`engine` fixtures would fail for a reason that has nothing to do with `ExecuteTailoringRun`.
This use case is tested against the ports it actually depends on: in-memory fakes of
`TailoringRunRepository`, `BaseCvRepository`, `JobPostingRepository` and `LlmPort`, from
`tests/integration/fakes.py`. Unlike `RequestTailoringRun`'s tests, no `GuestSessionRepository` is
needed here at all — `ExecuteTailoringRun.__init__` does not take one, because (per its own
docstring) the worker is not acting on behalf of a caller who might not own these rows; the run it
loads already encodes the authorization decision made at request time. A `GuestSessionId` below is
therefore just a value used to build a `TailoringRun`, never looked up.

**Why this file matters more than most.** `ExecuteTailoringRun` catches `TailoringFailed` and records
it as a state of the aggregate (ADR-0004, ADR-0014 §2) — the deliberate *opposite* of
`CaptureJobPosting`, which lets `JobPostingFetchFailed` propagate (ADR-0013). A reader arriving from
1.2 may "fix" this into a propagating error to match. Every failure-class assertion below therefore
checks the **recording** — the outcome `ExecuteTailoringRun.__call__` returns, the run's `status`,
and its `failure_reason` — rather than merely wrapping the call in "did not raise". A use case that
silently swallowed the failure and recorded nothing would still pass a "did not raise" test and would
fail every assertion written here, which is the whole point of writing it this way.

Every assertion below states what `__call__` **should** do per technical-plan.md's "Flow" section
(the seven steps under "2. `execute_tailoring_run.py`") and feature-spec.md's failure contract rows
G-16 … G-28, never what the (currently `NotImplementedError`) code was observed doing.

**The "running committed before the call" mechanism.** Step 4 requires `mark_started` to be saved and
committed in its own transaction *before* the LLM is ever called (technical-plan.md's "Step 4 — two
commits"), so that a polling client sees `running` during a twelve-second call rather than jumping
straight from `queued` to a terminal status. Proving that ordering needs a fake that can observe the
repository's state at the exact moment the LLM is invoked, which neither fake had before this file:
`FakeTailoringRunRepository.save_calls` (added below) records the status recorded by each `save()`
call, and `FakeLlm.on_call` (added below) is a synchronous hook invoked the instant `tailor()` starts.
Wiring the hook to snapshot `runs.save_calls` at that moment turns "was `running` already saved when
the model was asked?" into a plain list-equality assertion — one that fails immediately if a GREEN
implementation ever moved `mark_started`/`save` to after the LLM call.

**The idempotency test (AC-10) is deliberately a *second call* to the same use case**, simulating
Celery's `task_acks_late=True` redelivering the same run id, rather than only inspecting a
pre-decided run. `task_acks_late=True` is what makes redelivery real rather than theoretical, and the
guarantee that a redelivery cannot buy a second Gemini call comes from the **aggregate** (TR-3,
`TailoringAlreadyDecided`) — `mark_started` refuses to run twice — not from a flag on the task or in
this use case. Calling `__call__` twice against the same fakes is what actually exercises that
refusal instead of assuming it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRun,
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.errors import (
    LlmError,
    LlmInputsTooLarge,
    LlmOutputInvalid,
    LlmRateLimited,
    LlmRefused,
    LlmTimedOut,
    LlmUnavailable,
    TailoringFailed,
)
from tailorcraft.domain.tailoring.events import (
    TailoringRunFailed,
    TailoringRunStarted,
    TailoringRunSucceeded,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoredDraft,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeJobPostingRepository,
    FakeLlm,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
)

# --- Test helpers --------------------------------------------------------------------------------

# A single, well-formed `FileRef` key (the grammar in `domain/shared/files.py`) reused by every
# `BaseCv` this module builds — its content is irrelevant to every test here, only its existence.
_A_FILE_REF = FileRef(key="01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf")

# A fixed instant for building `BaseCv`/`JobPosting` fixtures. None of this use case's invariants
# compare a CV's `uploaded_at` or a posting's `created_at` against anything — only `TailoringRun`'s
# own timestamps matter here (`requested_at`, `started_at`, `completed_at`), and those are always
# built from the real `clock` fixture in each test. So the two document builders below take no
# clock at all; threading one through for a value nothing checks would only be noise at every call
# site.
_A_CLOCK_INSTANT = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def _a_session_id() -> GuestSessionId:
    """`ExecuteTailoringRun` never looks a session up — it takes no `GuestSessionRepository` at all
    (see this module's docstring) — so a fresh id with nothing behind it is exactly as good as a real
    one for every test here."""
    return GuestSessionId(value=uuid4())


def _extracted_text(marker: str = "x") -> ExtractedText:
    """220 non-whitespace characters — comfortably past `ExtractedText`'s 200-character floor."""
    return ExtractedText(marker * 220)


def _posting_text(marker: str = "p") -> JobPostingText:
    """150 non-whitespace characters — comfortably past `JobPostingText`'s 100-character floor and
    under its 30,000-character ceiling."""
    return JobPostingText(marker * 150)


def _uploaded_base_cv(session_id: GuestSessionId) -> BaseCv:
    """A `BaseCv` still in `UPLOADED` — `extracted_text is None`. Used only by the `cv_text_missing`
    test, to stand in for "the CV changed underneath us between the request and the pickup": this
    use case never checks `BaseCv.status`, it reads `extracted_text` directly, so a CV in any
    non-extracted state exercises the same branch."""
    return BaseCv.upload(
        BaseCvId(value=uuid4()),
        session_id,
        OriginalFilename("cv.pdf"),
        CvContentType.PDF,
        1024,
        _A_FILE_REF,
        _A_CLOCK_INSTANT,
    )


def _extracted_base_cv(session_id: GuestSessionId) -> BaseCv:
    """A `BaseCv` in `EXTRACTED` — the ordinary case this use case expects to find."""
    cv = BaseCv.upload(
        BaseCvId(value=uuid4()),
        session_id,
        OriginalFilename("cv.pdf"),
        CvContentType.PDF,
        1024,
        _A_FILE_REF,
        _A_CLOCK_INSTANT,
    )
    cv.mark_extracted(_extracted_text(), _A_CLOCK_INSTANT)
    return cv


def _job_posting(session_id: GuestSessionId) -> JobPosting:
    return JobPosting.from_pasted_text(
        id=JobPostingId(value=uuid4()),
        guest_session_id=session_id,
        text=_posting_text(),
        created_at=_A_CLOCK_INSTANT,
    )


def _queued_run(
    *,
    session_id: GuestSessionId,
    base_cv_id: BaseCvId,
    job_posting_id: JobPostingId,
    at: FixedClock,
) -> TailoringRun:
    return TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=base_cv_id,
        job_posting_id=job_posting_id,
        requested_at=at.now(),
    )


def _a_draft(cv_marker: str = "x", letter_marker: str = "y") -> TailoredDraft:
    """A well-formed `TailoredDraft` — comfortably past both documents' floors, comfortably under
    both ceilings — for every test that needs the LLM to succeed."""
    return TailoredDraft(
        documents=TailoredDocuments(
            cv=TailoredCv(cv_marker * 500), cover_letter=CoverLetter(letter_marker * 300)
        ),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1500,
        ),
    )


def _use_case(
    runs: FakeTailoringRunRepository,
    cvs: FakeBaseCvRepository,
    postings: FakeJobPostingRepository,
    llm: FakeLlm,
    events: RecordingEventPublisher,
    clock: FixedClock,
    stale_after_seconds: int = 300,
) -> ExecuteTailoringRun:
    return ExecuteTailoringRun(runs, cvs, postings, llm, events, clock, stale_after_seconds)


# --- 1. Happy path ---------------------------------------------------------------------------------


async def test_happy_path_records_success_and_publishes_the_succeeded_event(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    await runs.add(run)
    events = RecordingEventPublisher()
    draft = _a_draft()
    llm = FakeLlm(draft)
    use_case = _use_case(runs, cvs, postings, llm, events, clock)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(llm.calls) == 1
    assert llm.calls[0] == (cv.extracted_text, posting.text)

    stored = await runs.get(run.id)
    assert stored.status is TailoringRunStatus.SUCCEEDED
    assert stored.documents == draft.documents
    assert stored.metrics == draft.metrics
    assert stored.completed_at == clock.now()
    assert stored.failure_reason is None

    succeeded_events = [e for e in events.published if isinstance(e, TailoringRunSucceeded)]
    assert len(succeeded_events) == 1
    event = succeeded_events[0]
    assert event.tailoring_run_id == run.id
    assert event.model == draft.metrics.model
    assert event.prompt_version == draft.metrics.prompt_version
    assert event.prompt_tokens == draft.metrics.prompt_tokens
    assert event.completion_tokens == draft.metrics.completion_tokens
    assert event.duration_ms == draft.metrics.duration_ms
    assert event.cv_character_count == draft.documents.cv.character_count
    assert event.cover_letter_character_count == draft.documents.cover_letter.character_count


# --- 2. `running` is committed before the LLM is ever called (technical-plan.md, step 4) -----------


async def test_running_is_saved_before_the_llm_is_called(clock: FixedClock) -> None:
    """The two-transaction requirement: `mark_started` must be saved **before** `llm.tailor` is
    invoked, so a polling client sees `running` during the call rather than jumping straight from
    `queued` to a terminal status (technical-plan.md's "Step 4 — two commits").

    `FakeLlm.on_call` (added to `fakes.py` in this commit) is a synchronous hook invoked the instant
    `tailor()` starts, before it produces its configured outcome. Wiring it to snapshot
    `runs.save_calls` — also added in this commit — at that exact moment is what makes the assertion
    fail if a GREEN implementation ever moved the save to after the call: if `mark_started` were
    saved afterwards, `runs.save_calls` would still be empty when the hook fires.
    """
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    await runs.add(run)
    events = RecordingEventPublisher()

    observed_save_calls: list[tuple[TailoringRunStatus, ...]] = []
    llm = FakeLlm(_a_draft(), on_call=lambda: observed_save_calls.append(tuple(runs.save_calls)))
    use_case = _use_case(runs, cvs, postings, llm, events, clock)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    # exactly one save had happened by the time the LLM was called, and it recorded `running` —
    # not `queued` (never saved) and not `succeeded` (that save happens only after the call returns)
    assert observed_save_calls == [(TailoringRunStatus.RUNNING,)]


# --- 3. G-16 … G-23: every LLM failure is recorded, never propagated --------------------------------

_FAILURE_CASES = [
    pytest.param(LlmUnavailable(), TailoringFailureReason.LLM_UNAVAILABLE, id="G-16-unavailable"),
    pytest.param(LlmRateLimited(), TailoringFailureReason.LLM_RATE_LIMITED, id="G-17-rate_limited"),
    pytest.param(LlmRefused(), TailoringFailureReason.LLM_REFUSED, id="G-18-refused"),
    pytest.param(LlmTimedOut(), TailoringFailureReason.LLM_TIMED_OUT, id="G-19-timed_out"),
    pytest.param(
        LlmOutputInvalid("not_json"),
        TailoringFailureReason.LLM_OUTPUT_INVALID,
        id="G-20-21-output_invalid",
    ),
    pytest.param(
        LlmInputsTooLarge(), TailoringFailureReason.INPUTS_TOO_LARGE, id="G-22-inputs_too_large"
    ),
    pytest.param(LlmError(), TailoringFailureReason.LLM_ERROR, id="G-23-error"),
]


@pytest.mark.parametrize(("exc", "expected_reason"), _FAILURE_CASES)
async def test_llm_failure_is_recorded_on_the_run_and_never_propagates(
    clock: FixedClock, exc: TailoringFailed, expected_reason: TailoringFailureReason
) -> None:
    """The deliberate contrast with `CaptureJobPosting` (ADR-0013): `TailoringFailed` is caught here
    and turned into a state of the aggregate, so the assertions below are on the *recording* — the
    returned outcome, the run's `status` and `failure_reason` — never on "the call did not raise".
    A use case that swallowed the exception and recorded nothing would also "not raise", and would
    fail every assertion below, which is why they are all here rather than a single `try/except`."""
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    await runs.add(run)
    events = RecordingEventPublisher()
    llm = FakeLlm(exc)
    use_case = _use_case(runs, cvs, postings, llm, events, clock)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    stored = await runs.get(run.id)
    assert stored.status is TailoringRunStatus.FAILED
    assert stored.failure_reason is expected_reason
    assert stored.documents is None
    assert stored.metrics is None
    assert stored.completed_at == clock.now()

    failed_events = [e for e in events.published if isinstance(e, TailoringRunFailed)]
    assert len(failed_events) == 1
    assert failed_events[0].tailoring_run_id == run.id
    assert failed_events[0].reason is expected_reason


# --- 4. G-26: the run is gone (MISSING) -------------------------------------------------------------


async def test_unknown_run_id_returns_missing_without_raising(clock: FixedClock) -> None:
    runs = FakeTailoringRunRepository()  # empty: no run was ever added
    cvs = FakeBaseCvRepository()
    postings = FakeJobPostingRepository()
    events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    use_case = _use_case(runs, cvs, postings, llm, events, clock)

    outcome = await use_case(
        ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(value=uuid4()))
    )

    assert outcome is ExecuteTailoringRunOutcome.MISSING
    assert llm.calls == []
    assert events.published == []


# --- 5. AC-10: redelivery of an already-decided run is SKIPPED, no second LLM call ------------------


async def test_redelivery_of_an_already_decided_run_is_skipped_with_no_second_llm_call(
    clock: FixedClock,
) -> None:
    """The idempotency assertion (AC-10). `task_acks_late=True` makes redelivery of the same run id
    real rather than theoretical, so this test calls `__call__` **twice** against the same fakes —
    the first call runs the whole flow for real, the second simulates the redelivery — rather than
    only inspecting a run that was pre-decided by hand. The guarantee that the second call cannot buy
    a second Gemini call comes from the aggregate (TR-3): `mark_started` raises
    `TailoringAlreadyDecided` once the run is `succeeded`, which is what step 2 turns into `SKIPPED`
    before the LLM is ever reached — not from a flag on this use case or the Celery task.
    """
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    await runs.add(run)
    events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    use_case = _use_case(runs, cvs, postings, llm, events, clock)
    cmd = ExecuteTailoringRunCommand(tailoring_run_id=run.id)

    first = await use_case(cmd)
    assert first is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(llm.calls) == 1
    decided = await runs.get(run.id)
    assert decided.status is TailoringRunStatus.SUCCEEDED
    decided_documents = decided.documents

    second = await use_case(cmd)  # simulated redelivery of the same task

    assert second is ExecuteTailoringRunOutcome.SKIPPED
    assert len(llm.calls) == 1  # unchanged: no second call to the model
    unchanged = await runs.get(run.id)
    assert unchanged.status is TailoringRunStatus.SUCCEEDED
    assert unchanged.documents == decided_documents  # the recorded outcome did not change


# --- 6. G-27: a fresh `running` run, still inside the stale window, is SKIPPED ----------------------


async def test_fresh_running_run_is_skipped_with_no_llm_call(clock: FixedClock) -> None:
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    run.mark_started(clock.now())  # a worker is (as far as this test is concerned) mid-call now
    await runs.add(run)
    events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    use_case = _use_case(runs, cvs, postings, llm, events, clock, stale_after_seconds=300)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert llm.calls == []
    unchanged = await runs.get(run.id)
    assert unchanged.status is TailoringRunStatus.RUNNING
    assert unchanged.failure_reason is None


# --- 7. G-25: a `running` run past the stale window is ABANDONED ------------------------------------


async def test_running_run_past_the_stale_window_is_recorded_abandoned(clock: FixedClock) -> None:
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    run.mark_started(clock.now())
    await runs.add(run)
    clock.advance(301)  # past a 300-second stale window
    events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    use_case = _use_case(runs, cvs, postings, llm, events, clock, stale_after_seconds=300)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.ABANDONED
    assert llm.calls == []
    stored = await runs.get(run.id)
    assert stored.status is TailoringRunStatus.FAILED
    assert stored.failure_reason is TailoringFailureReason.ABANDONED
    assert stored.completed_at == clock.now()

    failed_events = [e for e in events.published if isinstance(e, TailoringRunFailed)]
    assert len(failed_events) == 1
    assert failed_events[0].reason is TailoringFailureReason.ABANDONED


# --- 8. The `cv_text_missing` impossibility ----------------------------------------------------------


async def test_missing_extracted_text_records_llm_error_without_calling_the_llm(
    clock: FixedClock,
) -> None:
    """`cv.extracted_text` is `ExtractedText | None` on the aggregate; step 3 of `RequestTailoringRun`
    already guaranteed it is not `None` at request time, but this use case must still narrow it. If
    it *is* `None` here, the CV changed underneath us — a genuine impossibility, not a contingency —
    and the technical plan is explicit that the reason recorded is `LLM_ERROR`, **not**
    `INPUTS_TOO_LARGE`: nothing was measured and nothing was too large, so a reason chosen because it
    was nearby would misreport why the run failed. No LLM call is made on this path."""
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _uploaded_base_cv(session_id)  # `extracted_text is None`
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)
    runs = FakeTailoringRunRepository()
    run = _queued_run(session_id=session_id, base_cv_id=cv.id, job_posting_id=posting.id, at=clock)
    await runs.add(run)
    events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    use_case = _use_case(runs, cvs, postings, llm, events, clock)

    outcome = await use_case(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    assert llm.calls == []  # the impossibility is caught before any call to the model
    stored = await runs.get(run.id)
    assert stored.status is TailoringRunStatus.FAILED
    assert stored.failure_reason is TailoringFailureReason.LLM_ERROR


# --- 9. AC-8: two deliveries of one queued run in flight at once (the ADR-0014 §6 residual) --------


async def test_two_concurrent_deliveries_the_loser_is_skipped_with_one_llm_call(
    clock: FixedClock,
) -> None:
    """AC-8: the duplicate-delivery gap ADR-0014's amendment left open, closed by ADR-0015 §3's
    version check. This is the mirror of 1.3's AC-10 (`test_redelivery_of_an_already_decided_run_is_
    skipped_with_no_second_llm_call`, above), which covers *sequential* redelivery — one worker,
    finished, then asked again — via the aggregate's own `TailoringAlreadyDecided` (TR-3). This test
    covers two deliveries **in flight at the same time**, which TR-3 cannot see: both read the run
    while it is still `queued`, so both `mark_started` calls succeed against their own in-memory
    copy, and only the version each carries into `save` can tell them apart.

    Two independent `TailoringRun` **objects**, not two references to one, is the load-bearing setup
    detail: sharing one Python object (as the two-fake in-memory store would if `find`/`get` handed
    back the same reference twice) would make the second `mark_started` raise `TailoringAlreadyStarted`
    in-process before either ever reached `save`, and this test would then be exercising 1.3's guard
    again instead of the new one. Building the run twice through `TailoringRun.request` with the same
    id, references and `requested_at` produces two distinct objects that each independently believe
    they are the first to start it — exactly what two separate worker processes, each with their own
    database session, would load.

    Against 1.3's unmodified `execute_tailoring_run.py` this must go red: nothing there catches
    `TailoringRunConcurrentlyModified`, so the loser's `__call__` propagates the fake's raised
    exception instead of returning `SKIPPED`.
    """
    session_id = _a_session_id()
    cvs = FakeBaseCvRepository()
    cv = _extracted_base_cv(session_id)
    await cvs.add(cv)
    postings = FakeJobPostingRepository()
    posting = _job_posting(session_id)
    await postings.add(posting)

    run_id = TailoringRunId(value=uuid4())
    requested_at = clock.now()

    def _independently_loaded_copy() -> TailoringRun:
        return TailoringRun.request(
            id=run_id,
            guest_session_id=session_id,
            base_cv_id=cv.id,
            job_posting_id=posting.id,
            requested_at=requested_at,
        )

    winner_runs = FakeTailoringRunRepository()
    await winner_runs.add(_independently_loaded_copy())
    # The loser's very own `save` — its first and only one in this test — is the one that must lose
    # the race, so `conflict_on_save=1` fires exactly there.
    loser_runs = FakeTailoringRunRepository(conflict_on_save=1)
    await loser_runs.add(_independently_loaded_copy())

    winner_events = RecordingEventPublisher()
    loser_events = RecordingEventPublisher()
    llm = FakeLlm(_a_draft())
    winner_use_case = _use_case(winner_runs, cvs, postings, llm, winner_events, clock)
    loser_use_case = _use_case(loser_runs, cvs, postings, llm, loser_events, clock)
    cmd = ExecuteTailoringRunCommand(tailoring_run_id=run_id)

    winner_outcome = await winner_use_case(cmd)
    assert winner_outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(llm.calls) == 1

    loser_outcome = await loser_use_case(cmd)

    assert loser_outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert len(llm.calls) == 1  # unchanged: the loser never reached the model
    # the loser's `mark_started` recorded `TailoringRunStarted` on its own in-memory copy, but its
    # `save` raised before step 4's `publish`, so that event never left the buffer
    loser_started_events = [e for e in loser_events.published if isinstance(e, TailoringRunStarted)]
    assert loser_started_events == []
