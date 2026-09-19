"""Builders shared by every `export` application test file (T6).

Not `tests/integration/fakes.py`: these are not `Protocol` stand-ins, they are `TailoringRun`
factories built the only legal way — through `request` → `mark_started` → `mark_succeeded` (and
occasionally `revise_cv`), never by touching a private attribute — the same discipline
`test_revise_tailored_document.py`'s and `test_abandon_stale_tailoring_runs.py`'s local `_succeeded_
run` / `_running_run` helpers keep. They are pulled into one module here, rather than duplicated
seven times across this package the way those two files duplicate each other's, because every file
below needs the identical shape (a `succeeded` run to export from) and a hand-copied seventh version
is exactly how a fixture drifts unnoticed.

`CLOCK_NOW` matches `tests/conftest.py`'s session-scoped `clock` fixture
(`FixedClock(datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC))`) exactly, so every builder below can place
a run's timestamps safely before "now" without threading the fixture through each one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
)

CLOCK_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def a_documents(cv_marker: str = "d", letter_marker: str = "d") -> TailoredDocuments:
    """400 non-whitespace characters of CV, 200 of cover letter — comfortably past both floors."""
    return TailoredDocuments(
        cv=TailoredCv(cv_marker * 400), cover_letter=CoverLetter(letter_marker * 200)
    )


def a_metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


def succeeded_run(
    *,
    session_id: GuestSessionId,
    requested_at: datetime = CLOCK_NOW - timedelta(minutes=15),
    started_at: datetime = CLOCK_NOW - timedelta(minutes=10),
    completed_at: datetime = CLOCK_NOW - timedelta(minutes=5),
) -> TailoringRun:
    """A `succeeded` run — the only status this slice ever exports from — at `version == 3`
    (`request` → `mark_started` → `mark_succeeded`, each bumping `version` by one, TR-8). Every
    instant defaults comfortably before `CLOCK_NOW`, so a job requested "now" always satisfies
    `ExportJob.request`'s XJ-8 floor and every transition's XJ-5 ordering.
    """
    run = TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=requested_at,
    )
    run.mark_started(started_at)
    run.mark_succeeded(a_documents(), a_metrics(), completed_at)
    return run


def independently_loaded_succeeded_run_pair(
    *, session_id: GuestSessionId
) -> tuple[TailoringRun, TailoringRun]:
    """Two distinct `TailoringRun` **objects** describing the same succeeded run, built through the
    identical sequence of calls with the same id — the load-bearing detail `test_execute_tailoring_
    run.py`'s `test_two_concurrent_deliveries_the_loser_is_skipped_with_one_llm_call` explains at
    length: two separate worker processes each load their own copy, so a race test must too, or it
    exercises an aggregate-level refusal instead of the repository-level one under test.
    """

    def _copy() -> TailoringRun:
        run = TailoringRun.request(
            id=run_id,
            guest_session_id=session_id,
            base_cv_id=base_cv_id,
            job_posting_id=job_posting_id,
            requested_at=requested_at,
        )
        run.mark_started(started_at)
        run.mark_succeeded(a_documents(), a_metrics(), completed_at)
        return run

    run_id = TailoringRunId(value=uuid4())
    base_cv_id = BaseCvId(value=uuid4())
    job_posting_id = JobPostingId(value=uuid4())
    requested_at = CLOCK_NOW - timedelta(minutes=15)
    started_at = CLOCK_NOW - timedelta(minutes=10)
    completed_at = CLOCK_NOW - timedelta(minutes=5)
    return _copy(), _copy()


def queued_run(*, session_id: GuestSessionId) -> TailoringRun:
    return TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=CLOCK_NOW - timedelta(minutes=5),
    )


def running_run(*, session_id: GuestSessionId) -> TailoringRun:
    run = queued_run(session_id=session_id)
    run.mark_started(CLOCK_NOW - timedelta(minutes=4))
    return run


def failed_run(*, session_id: GuestSessionId) -> TailoringRun:
    run = running_run(session_id=session_id)
    run.mark_failed(TailoringFailureReason.LLM_ERROR, CLOCK_NOW - timedelta(minutes=3))
    return run
