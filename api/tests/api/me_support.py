"""Shared helpers for slice 2.3's account-route API tests (T21/T22).

Every `/api/me/job-postings*`, `/api/me/tailoring-runs*` and `/api/me/export-jobs*` handler is a T20
skeleton (`NotImplementedError`), so the rows those tests read are **seeded through the real
repositories** on the test's own `session` — the same session the `app` fixture serves requests
from — and never through the routes under test, except in the tests that are *about* a create
route. Users are registered through the real 2.1 endpoint, so the bearer is the exact token
`require_user` sees in production; guests are minted through the real 1.1 upload, so the cookie is
too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.deps import get_app_settings
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import (
    extracted_cv,
    pasted_posting,
    queued_export,
    ready_export,
    succeeded_run,
)

ME_POSTINGS = "/api/me/job-postings"
ME_RUNS = "/api/me/tailoring-runs"
ME_EXPORT_JOBS = "/api/me/export-jobs"
ME_BASE_CVS = "/api/me/base-cvs"
REGISTER_URL = "/api/auth/register"
DELETE_ACCOUNT_URL = "/api/auth/delete-account"
A_PASSWORD = "correct horse battery staple 9"
SAMPLE_CV = (
    Path(__file__).resolve().parent.parent / "fixtures" / "cvs" / "sample.txt"
).read_bytes()

JOB_POSTING_RESPONSE_KEYS = {
    "id",
    "source",
    "source_url",
    "title",
    "character_count",
    "text",
    "created_at",
    "expires_at",
}
JOB_POSTING_SUMMARY_KEYS = {
    "id",
    "source",
    "source_url",
    "title",
    "character_count",
    "preview",
    "created_at",
    "expires_at",
}
TAILORING_RUN_RESPONSE_KEYS = {
    "id",
    "status",
    "base_cv_id",
    "job_posting_id",
    "failure_reason",
    "retryable",
    "tailored_cv",
    "cover_letter",
    "tailored_cv_character_count",
    "cover_letter_character_count",
    "model",
    "prompt_version",
    "llm_duration_ms",
    "requested_at",
    "started_at",
    "completed_at",
    "expires_at",
    "version",
    "tailored_cv_edited_at",
    "cover_letter_edited_at",
}
HISTORY_ENTRY_KEYS = {
    "id",
    "status",
    "failure_reason",
    "retryable",
    "requested_at",
    "completed_at",
    "version",
    "edited",
    "base_cv_id",
    "base_cv",
    "posting",
}
HISTORY_BASE_CV_KEYS = {"id", "label", "original_filename"}
HISTORY_POSTING_KEYS = {"id", "source", "title", "source_url", "preview"}
EXPORT_JOB_RESPONSE_KEYS = {
    "id",
    "tailoring_run_id",
    "document",
    "format",
    "status",
    "failure_reason",
    "retryable",
    "run_version",
    "current",
    "byte_size",
    "render_duration_ms",
    "file_url",
    "requested_at",
    "started_at",
    "completed_at",
    "expires_at",
}

# AC-30/AC-55: the document columns no list statement may select. `(?!_)` keeps
# `cover_letter_edited_at` / `cv_edited_at` (instants, not bodies) out of the match.
DOCUMENT_COLUMNS = re.compile(
    r"\b(tailored_cv|cover_letter|edited_cv|edited_cover_letter|extracted_text)\b(?!_)"
)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def error_body(response: Response) -> dict[str, object]:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return dict(body["error"])


def new_client(app: FastAPI) -> AsyncClient:
    """`raise_app_exceptions=False`: a skeleton's `NotImplementedError` is a real 500 response, so
    a red reads `assert 500 == 200` rather than an ERROR."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


def override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """Both settings paths overridden together (`test_intake.py`'s helper). `model_copy` does not
    validate, which is what lets a per-user cap drop below its `ge=1`-bounded production range."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a committing test against {settings.database_url!r}"
    )


@dataclass
class Account:
    token: str
    owner: UserOwner

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)

    @property
    def user_id(self) -> UserId:
        return self.owner.user_id


async def register(client: AsyncClient, settings: Settings) -> Account:
    response = await client.post(
        REGISTER_URL,
        json={"email": f"t21-{uuid4().hex}@example.com", "password": A_PASSWORD},
        headers={"Origin": settings.public_base_url},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Account(str(body["access_token"]), UserOwner(UserId(UUID(body["user"]["id"]))))


async def mint_guest(client: AsyncClient, session: AsyncSession) -> GuestOwner:
    """A real guest session: a real 1.1 upload sets `tc_guest` on `client`; the session id is read
    back off the CV row it created."""
    response = await client.post(
        "/api/base-cvs", files={"file": ("sample.txt", SAMPLE_CV, "text/plain")}
    )
    assert response.status_code == 201, response.text
    sid = (
        await session.execute(
            select(base_cv_table.c.guest_session_id).where(
                base_cv_table.c.id == BaseCvId(UUID(response.json()["id"]))
            )
        )
    ).scalar_one()
    assert isinstance(sid, GuestSessionId)
    return GuestOwner(sid)


@dataclass(frozen=True)
class SeededJob:
    id: ExportJobId
    storage_ref: FileRef
    format: ExportFormat


@dataclass
class Entry:
    """One seeded history entry: a saved CV, a posting, a run over both, its export jobs.

    **Plain values, snapshotted at seeding time**, not reads through the aggregates: every request
    that ends in a rollback (a skeleton's 500 today, every 404 tomorrow) expires the instances the
    shared session holds, and a later `run.id` would be a lazy load — `MissingGreenlet` on an
    `AsyncSession`, a failure that says nothing about the code under test. `run` is kept for
    set-up work done *before* the first request only.
    """

    cv_id: BaseCvId
    posting_id: JobPostingId
    posting_preview: str
    run_id: TailoringRunId
    run_version: int
    jobs: list[SeededJob]
    run: TailoringRun

    @property
    def run_url(self) -> str:
        return f"{ME_RUNS}/{self.run_id.value}"


async def seed_entry(
    session: AsyncSession,
    settings: Settings,
    owner: Owner,
    *,
    at: datetime,
    run: TailoringRun | None = None,
    ready_formats: tuple[ExportFormat, ...] = (ExportFormat.PDF,),
) -> Entry:
    """A CV and a posting owned by `owner`, a run over them (succeeded unless `run` is given), and
    one `ready` export per format whose bytes are on the real upload volume at the job's key."""
    cv = extracted_cv(owner, at)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    posting = pasted_posting(owner, at)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    the_run = run or succeeded_run(owner, at, base_cv_id=cv.id, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(the_run)
    jobs: list[SeededJob] = []
    files = LocalFileStore(settings.upload_dir)
    for export_format in ready_formats:
        job = ready_export(owner, the_run, at, format=export_format)
        await SqlAlchemyExportJobRepository(session).add(job)
        await files.put(job.storage_ref, export_bytes(job.id))
        jobs.append(SeededJob(job.id, job.storage_ref, job.format))
    await session.flush()
    return Entry(
        cv_id=cv.id,
        posting_id=posting.id,
        posting_preview=posting.text.value[:140],
        run_id=the_run.id,
        run_version=the_run.version,
        jobs=jobs,
        run=the_run,
    )


async def seed_queued_export(
    session: AsyncSession, owner: Owner, run: TailoringRun, at: datetime
) -> ExportJob:
    job = queued_export(
        owner, run, at, document=TailoredDocumentKind.COVER_LETTER, format=ExportFormat.DOCX
    )
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.flush()
    return job


def export_bytes(job_id: ExportJobId) -> bytes:
    return f"%PDF-1.7 export {job_id.value}".encode()


def earlier(now: datetime, minutes: int = 10) -> datetime:
    return now - timedelta(minutes=minutes)


async def count_rows(session: AsyncSession, table: str, **where: object) -> int:
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "TRUE"
    result = await session.execute(
        text(f"SELECT count(*) FROM {table} WHERE {clause}"),  # noqa: S608 -- test-owned identifiers
        where,
    )
    return int(result.scalar_one())


@contextmanager
def captured_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)
