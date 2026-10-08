"""Row builders for slice 2.3's application tests (T10) — one of each aggregate, owned by either
variant of `Owner`.

Each builder goes the only legal way through its aggregate's named constructors and transitions,
releases the recorded events (a stand-in for a *loaded* aggregate must hold none — 2.2's "tests that
tested the harness" note: a pending event once leaked into a publisher this way), and takes its
instants from the caller, so a test's `FixedClock` stays the one source of time. Nothing here
decides an owner: every builder is handed one, which is the point of the slice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, LayoutTemplate
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import Owner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocumentKind,
    TailoredDocuments,
    TailoredDraft,
    TailoringFailureReason,
    TailoringRunId,
)
from tests.integration.fakes import FakeGuestSessionRepository, FakeUserRepository

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


async def seed_user(users: FakeUserRepository, email: str, at: datetime) -> UserOwner:
    """Register a user in `users` and return it as the `UserOwner` a bearer token would carry."""
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), _HASH, at=at
    )
    user.release_events()
    await users.add(user)
    return UserOwner(user.id)


async def seed_expired_session(
    sessions: FakeGuestSessionRepository, token_hash: str, now: datetime
) -> GuestSession:
    """A session that exists but expired 24 hours before `now`."""
    session = GuestSession.start(
        id=sessions.next_identity(),
        token_hash=token_hash.ljust(64, "0"),
        at=now - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(session)
    return session


def uploaded_cv(owner: Owner, at: datetime) -> BaseCv:
    """An `UPLOADED` base CV — its extraction not yet decided."""
    cv_id = BaseCvId(value=uuid4())
    cv = BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1024,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=at,
    )
    cv.release_events()
    return cv


def extracted_cv(owner: Owner, at: datetime) -> BaseCv:
    """An `EXTRACTED` base CV owned by `owner` — for a `UserOwner`, a saved CV."""
    cv = uploaded_cv(owner, at)
    cv.mark_extracted(ExtractedText("word " * 60), at)
    cv.release_events()
    return cv


def extraction_failed_cv(owner: Owner, at: datetime) -> BaseCv:
    cv = uploaded_cv(owner, at)
    cv.mark_extraction_failed(ExtractionFailureReason.EXTRACTOR_ERROR, at)
    cv.release_events()
    return cv


def pasted_posting(owner: Owner, at: datetime, *, marker: str = "p") -> JobPosting:
    posting = JobPosting.from_pasted_text(
        id=JobPostingId(value=uuid4()),
        owner=owner,
        text=JobPostingText(f"{marker}ost " * 40),
        created_at=at,
    )
    posting.release_events()
    return posting


def a_draft() -> TailoredDraft:
    return TailoredDraft(
        documents=TailoredDocuments(cv=TailoredCv("c" * 500), cover_letter=CoverLetter("l" * 300)),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
        ),
    )


def queued_run(
    owner: Owner,
    requested_at: datetime,
    *,
    base_cv_id: BaseCvId | None = None,
    job_posting_id: JobPostingId | None = None,
    run_id: TailoringRunId | None = None,
) -> TailoringRun:
    run = TailoringRun.request(
        id=run_id if run_id is not None else TailoringRunId(value=uuid4()),
        owner=owner,
        base_cv_id=base_cv_id if base_cv_id is not None else BaseCvId(value=uuid4()),
        job_posting_id=(
            job_posting_id if job_posting_id is not None else JobPostingId(value=uuid4())
        ),
        requested_at=requested_at,
    )
    run.release_events()
    return run


def running_run(owner: Owner, requested_at: datetime, **ids: object) -> TailoringRun:
    run = queued_run(owner, requested_at, **ids)  # type: ignore[arg-type]
    run.mark_started(requested_at)
    run.release_events()
    return run


def succeeded_run(owner: Owner, requested_at: datetime, **ids: object) -> TailoringRun:
    run = running_run(owner, requested_at, **ids)
    draft = a_draft()
    run.mark_succeeded(draft.documents, draft.metrics, requested_at)
    run.release_events()
    return run


def failed_run(owner: Owner, requested_at: datetime, **ids: object) -> TailoringRun:
    run = running_run(owner, requested_at, **ids)
    run.mark_failed(TailoringFailureReason.LLM_ERROR, requested_at)
    run.release_events()
    return run


def queued_export(
    owner: Owner,
    run: TailoringRun,
    at: datetime,
    *,
    document: TailoredDocumentKind = TailoredDocumentKind.CV,
    format: ExportFormat = ExportFormat.PDF,
) -> ExportJob:
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        owner=owner,
        tailoring_run_id=run.id,
        document=document,
        format=format,
        layout_template=LayoutTemplate.CLASSIC if format is ExportFormat.PDF else None,
        run_version=run.version,
        requested_at=at,
    )
    job.release_events()
    return job


def ready_export(
    owner: Owner,
    run: TailoringRun,
    at: datetime,
    *,
    format: ExportFormat = ExportFormat.PDF,
    byte_size: int = 10,
) -> ExportJob:
    job = queued_export(owner, run, at, format=format)
    job.mark_started(at)
    job.mark_ready(byte_size=byte_size, render_duration_ms=10, at=at)
    job.release_events()
    return job
