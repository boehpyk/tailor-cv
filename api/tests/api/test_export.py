"""API tests for the queued export pair: `POST/GET /api/tailoring-runs/{id}/exports`,
`GET /api/export-jobs/{id}[/file]`.

**This is the RED half of a red-first cycle** (CLAUDE.md, docs/sdlc.md §2, I17). Every test here is
written against `docs/specs/export-multi-format-download/feature-spec.md`'s failure contract
(X-10…X-23, X-41…X-50) and acceptance criteria AC-12…AC-19, AC-24…AC-26, AC-32, AC-48 — not against
`infrastructure/api/routers/export.py`, whose four remaining handlers are currently a single `raise
NotImplementedError` each (I16's SKELETON). Every router IS mounted, so a passing collection with
every behavioural test failing on a real assertion — never a bare `ImportError`, never a 404 from a
missing route, never a 422 from a broken signature — is what a correct RED run looks like here.

**Runs are built directly through the aggregate and the repository**, exactly as
`test_export_inline.py` and `test_tailoring_revise.py` both already do, and for the identical
reason: this router's four handlers are what is under test, and a worker round-trip through
`POST /api/tailoring-runs` would only add noise between the setup and the assertion.

**The queued render itself is driven in-process**, the same technique `test_tailoring.py`'s
`_run_worker` uses one aggregate over: `RenderExportJob` — the same use case
`infrastructure/tasks/export.py::render_export` calls — built directly against *this test's own*
`session` rather than a brand-new engine and connection (`tasks/container.py::export_use_case`
opens one of those per real task, and a second, genuinely separate connection cannot see a row this
suite's own transaction never actually commits at the Postgres wire level — `test_tailoring.py`'s
module docstring gives the same argument in full). `_run_export_worker` below calls
`RenderExportJob` directly, with a `FakeDocumentRenderer` and an `InMemoryFileStore` standing in for
the real adapter and the real volume — AC-17 asks for exactly this pair by name.

**Why this file's `client` fixture is not the shared one, and why every `POST` test requests
`clear_redis`.** Both for the reasons `test_tailoring.py` and `test_export_inline.py` already give:
`ASGITransport`'s default `raise_app_exceptions=True` would turn a skeleton's `NotImplementedError`
into an aborted test rather than a real 500 response, and the `export:create` Redis namespace is
untouched by the per-test database rollback — a leftover counter from an earlier test would silently
change a later one's rate-limit assertions.

**What a "legitimate red" looks like in this file.** Every handler's body is still `raise
NotImplementedError`, and `main.py` registers no handler for a bare one, so it propagates through
`ServerErrorMiddleware` into a plain 500 with no JSON envelope — `raise_app_exceptions=False` turns
that into a real response rather than an aborted test. Every behavioural assertion below therefore
fails today at `assert response.status_code == <real code>` seeing `500` instead: a genuine
assertion failure, never a 404 (every route exists) and never a 422 from a broken signature (where
FastAPI's own validation applies, it already answers correctly today because it runs *before* the
handler body — I16's finding #1, reused from `test_export_inline.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.render_export_job import (
    RenderExportJob,
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.domain.export.errors import (
    DocumentRenderFailed,
)
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, LayoutTemplate
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocumentKind,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
)
from tailorcraft.infrastructure.api.deps import (
    get_app_settings,
    get_document_renderer,
    get_export_queue,
)
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.settings import Settings
from tests.integration.fakes import (
    AlwaysFailingFileStore,
    FakeDocumentRenderer,
    FakeExportQueue,
    InMemoryFileStore,
    MissingFileStore,
)

# ---------------------------------------------------------------------------------------------
# Fixture text
# ---------------------------------------------------------------------------------------------


def _cv_body_text(marker: str = "QA_EXPORT_CV_TOKEN") -> str:
    filler = "Senior backend engineer with a decade leading platform reliability work. " * 8
    return f"{marker} {filler}"


def _posting_body_text(marker: str = "QA_EXPORT_POSTING_TOKEN") -> str:
    filler = "We are hiring a senior engineer to own reliability and mentor the team. " * 4
    return f"{marker} {filler}"


def _tailored_cv_text(marker: str = "QA_EXPORT_DRAFT_CV_TOKEN") -> str:
    filler = "Rewrote the platform reliability program end to end for this role. " * 8
    return f"{marker} {filler}".rstrip()


def _cover_letter_text(marker: str = "QA_EXPORT_DRAFT_LETTER_TOKEN") -> str:
    filler = "I am applying because this role matches my reliability background. " * 6
    return f"{marker} {filler}".rstrip()


def _now_whole_second() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers
# ---------------------------------------------------------------------------------------------


def _error_code(response: Response) -> str:
    # A skeleton's bare `NotImplementedError` answers with Starlette's default plain-text 500 page,
    # not a JSON envelope — `response.json()` on that body raises `JSONDecodeError`, which pytest
    # reports as an unhandled exception rather than a real assertion failure (a weaker red than the
    # one this test means to record). Converted to `AssertionError` here so every caller's red is on
    # an assertion, never on a decode crash — the eventual check (`"error" in body`) is unchanged.
    try:
        body = response.json()
    except ValueError as exc:
        raise AssertionError(
            f"expected a JSON {{'error': {{...}}}} envelope, got a non-JSON "
            f"{response.status_code} body: {response.text[:200]!r}"
        ) from exc
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _guest_cookie_header(response: Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header
    return None


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="https://testserver"
    )


def _install_renderer(
    app: FastAPI, outcome: bytes | DocumentRenderFailed, *, delay_seconds: float = 0.0
) -> FakeDocumentRenderer:
    renderer = FakeDocumentRenderer(outcome, delay_seconds=delay_seconds)
    app.dependency_overrides[get_document_renderer] = lambda: renderer
    return renderer


def _install_queue(app: FastAPI) -> FakeExportQueue:
    queue = FakeExportQueue()
    app.dependency_overrides[get_export_queue] = lambda: queue
    return queue


def _install_failing_queue(app: FastAPI) -> FakeExportQueue:
    from tailorcraft.domain.export.errors import ExportNotQueued

    queue = FakeExportQueue(outcome=ExportNotQueued("simulated broker failure (X-22)"))
    app.dependency_overrides[get_export_queue] = lambda: queue
    return queue


# ---------------------------------------------------------------------------------------------
# Seeding — CV/posting through the real HTTP surfaces; the run itself built directly through the
# aggregate and the repository (see the module docstring).
# ---------------------------------------------------------------------------------------------


async def _mint_cookie(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs", files={"file": ("mint.txt", _cv_body_text("MINT").encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text


async def _upload_ready_base_cv(client: AsyncClient, *, text: str | None = None) -> str:
    body_text = text if text is not None else _cv_body_text()
    response = await client.post(
        "/api/base-cvs", files={"file": ("cv.txt", body_text.encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted", body
    return str(body["id"])


async def _create_ready_job_posting(client: AsyncClient, *, text: str | None = None) -> str:
    body_text = text if text is not None else _posting_body_text()
    response = await client.post("/api/job-postings", json={"source": "pasted", "text": body_text})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _ready_inputs(
    client: AsyncClient, *, cv_text: str | None = None, posting_text: str | None = None
) -> tuple[str, str]:
    base_cv_id = await _upload_ready_base_cv(client, text=cv_text)
    job_posting_id = await _create_ready_job_posting(client, text=posting_text)
    return base_cv_id, job_posting_id


async def _guest_session_id_for(session: AsyncSession, base_cv_id: str) -> UUID:
    result = await session.execute(
        sql_text("SELECT guest_session_id FROM intake_base_cv WHERE id = :id"),
        {"id": UUID(base_cv_id)},
    )
    return UUID(str(result.scalar_one()))


def _tailoring_repo(session: AsyncSession) -> object:
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return SqlAlchemyTailoringRunRepository(session)


def _export_repo(session: AsyncSession) -> object:
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )

    return SqlAlchemyExportJobRepository(session)


async def _create_queued_run(
    client: AsyncClient,
    session: AsyncSession,
    *,
    cv_text: str | None = None,
    posting_text: str | None = None,
) -> str:
    from tailorcraft.domain.tailoring.tailoring_run import TailoringRun

    base_cv_id, job_posting_id = await _ready_inputs(
        client, cv_text=cv_text, posting_text=posting_text
    )
    guest_session_id = await _guest_session_id_for(session, base_cv_id)

    repo = _tailoring_repo(session)
    run = TailoringRun.request(
        id=repo.next_identity(),  # type: ignore[attr-defined]
        owner=GuestOwner(GuestSessionId(guest_session_id)),
        base_cv_id=BaseCvId(UUID(base_cv_id)),
        job_posting_id=JobPostingId(UUID(job_posting_id)),
        requested_at=_now_whole_second(),
    )
    await repo.add(run)  # type: ignore[attr-defined]
    await session.commit()
    return str(run.id.value)


async def _advance_to_running(client: AsyncClient, session: AsyncSession) -> str:
    run_id = await _create_queued_run(client, session)
    repo = _tailoring_repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_started(_now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_failed_run(client: AsyncClient, session: AsyncSession) -> str:
    run_id = await _advance_to_running(client, session)
    repo = _tailoring_repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_failed(TailoringFailureReason.LLM_REFUSED, _now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_succeeded_run(
    client: AsyncClient,
    session: AsyncSession,
    *,
    cv_marker: str = "QA_EXPORT_DRAFT_CV_TOKEN",
    letter_marker: str = "QA_EXPORT_DRAFT_LETTER_TOKEN",
) -> str:
    run_id = await _advance_to_running(client, session)
    repo = _tailoring_repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_succeeded(
        TailoredDocuments(
            cv=TailoredCv(_tailored_cv_text(cv_marker)),
            cover_letter=CoverLetter(_cover_letter_text(letter_marker)),
        ),
        LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1234,
        ),
        _now_whole_second(),
    )
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _current_run_version(session: AsyncSession, run_id: str) -> int:
    """The row's live `version` (TR-8), read back rather than hard-coded — `test_tailoring_revise.py`
    establishes the identical helper (as raw SQL) for the same reason. `_create_succeeded_run`'s
    request -> start -> succeed sequence already bumps `version` to 3 before any of these tests
    revise anything, so a caller that hard-codes `expected_version=1` gets
    `TailoredDocumentVersionConflict` (409) out of its own setup, not out of the router under test."""
    repo = _tailoring_repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    return int(run.version)


async def _guest_session_id_for_run(session: AsyncSession, run_id: str) -> GuestSessionId:
    result = await session.execute(
        sql_text("SELECT guest_session_id FROM tailoring_run WHERE id = :id"), {"id": UUID(run_id)}
    )
    return GuestSessionId(UUID(str(result.scalar_one())))


async def _revise_cv(
    client: AsyncClient, run_id: str, *, content: str, expected_version: int
) -> int:
    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": content, "expected_version": expected_version},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["version"])


async def _post_export(
    client: AsyncClient, run_id: str, *, document: str = "cv", format: str = "pdf"
) -> Response:
    return await client.post(
        f"/api/tailoring-runs/{run_id}/exports", json={"document": document, "format": format}
    )


async def _count_export_jobs(session: AsyncSession) -> int:
    result = await session.execute(sql_text("SELECT count(*) FROM export_job"))
    return int(result.scalar_one())


async def _seed_jobs_for_cap(
    session: AsyncSession, run_id: str, guest_session_id: GuestSessionId, *, count: int
) -> None:
    """Build `count` `ExportJob` rows directly through the aggregate and the repository, bypassing
    `RequestExport`'s idempotent lookup entirely — X-18's cap is a raw `COUNT(*)` over every job a
    session owns, and this is the fast way to put a session at it without 40 real HTTP round trips
    through a rate limiter."""
    from tailorcraft.domain.export.export_job import ExportJob

    repo = _export_repo(session)
    now = _now_whole_second()
    for _ in range(count):
        job = ExportJob.request(
            id=repo.next_identity(),  # type: ignore[attr-defined]
            owner=GuestOwner(guest_session_id),
            tailoring_run_id=TailoringRunId(UUID(run_id)),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=LayoutTemplate.CLASSIC,
            run_version=1,
            requested_at=now,
        )
        await repo.add(job)  # type: ignore[attr-defined]
    await session.commit()


async def _run_export_worker(
    session: AsyncSession,
    settings: Settings,
    job_id: str,
    *,
    renderer: FakeDocumentRenderer,
    files: InMemoryFileStore | MissingFileStore | AlwaysFailingFileStore,
    stale_after_seconds: int | None = None,
) -> RenderExportJobOutcome:
    """Run `RenderExportJob` — the identical use case `infrastructure/tasks/export.py::render_export`
    calls — in-process, against *this test's own* `session`, with a fake renderer and an in-memory
    file store standing in for the real adapter and the real volume (AC-17). Mirrors
    `test_tailoring.py::_run_worker`'s reasoning in full: a brand-new engine and connection (what
    `tasks/container.py::export_use_case` opens for a real task) cannot see a row this suite's own
    transaction never actually commits at the Postgres wire level."""
    from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )
    from tailorcraft.infrastructure.tasks.container import CommittingExportJobRepository

    jobs = CommittingExportJobRepository(SqlAlchemyExportJobRepository(session), session)
    runs = _tailoring_repo(session)
    use_case = RenderExportJob(
        jobs=jobs,
        runs=runs,  # type: ignore[arg-type]
        renderer=renderer,
        files=files,
        events=LoggingEventPublisher(),
        clock=SystemClock(),
        stale_after_seconds=(
            stale_after_seconds
            if stale_after_seconds is not None
            else settings.export_stale_after_seconds
        ),
    )
    return await use_case(RenderExportJobCommand(export_job_id=ExportJobId(UUID(job_id))))


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Every `POST` test in this module needs this — `export:create`'s counters are not touched by
    the per-test database rollback (CLAUDE.md). Applied to every test, not only the `POST` ones, for
    the same reason `test_tailoring.py` applies it everywhere: a future test that starts posting
    should not have to remember to opt in."""
    return None


# ---------------------------------------------------------------------------------------------
# X-10 / AC-15 — malformed POST bodies: 422 validation_error
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-11 — the 256 KiB JSON body cap
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-12 — no cookie: 401, no session minted
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-13 — run missing or not mine: identical 404
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-14 — the run is not succeeded: 409 tailoring_run_not_exportable, body carries status
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# AC-12 — the happy path's shape: 202, ExportJobResponse, Location header
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-16 / AC-13(a) — a second identical POST while the job is in flight: 200, same id, no new row,
# no enqueue
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-17 / AC-13(b),(c) — both branches: the latest job failed, or the run's version moved on
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-18 — the session already owns the cap: 409 too_many_export_jobs, no job created
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-19 — rate limits: 30/hour/session, 60/hour/client-IP
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-20 — Redis unreachable: the export limiter FAILS OPEN (contrast with tailoring's fail-closed)
# ---------------------------------------------------------------------------------------------


async def _current_settings(app: FastAPI) -> Settings:
    """`app.state.settings`, typed — the `_override_settings` helper needs a base `Settings` to
    `model_copy` from, and this file's `settings` fixture is session-scoped so a second override in
    the same test must chain off whatever the first one already set."""
    settings = app.state.settings
    assert isinstance(settings, Settings)
    return settings


# ---------------------------------------------------------------------------------------------
# X-21 — the commit fails before the enqueue: 503 service_unavailable, nothing enqueued
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-22 — the enqueue fails after the row is committed: 503 queue_unavailable, the row survives,
# recorded failed/not_queued in a SECOND transaction
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-41 — a malformed export-job id: 422
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-42 / X-50 — no / unknown / expired cookie on any GET: 401
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-43 / AC-24 — job does not exist, or belongs to another session: identical 404
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-44 / X-45 / AC-26 — the file route on a queued/rendering/failed job: 409 export_not_ready
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-46 — a stale ready job's file is still served
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-47 / AC-26 — the file is missing from the store on a ready job: 410 export_file_gone
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# X-48 / AC-26 — the store is unreadable: 503 storage_unavailable (corrected from
# `service_unavailable` at I16 — the existing FileStoreUnavailable branch already maps this way)
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# AC-17 — the whole path with the fake renderer and the in-memory file store
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# AC-19 — every failure reason the worker can produce is a recorded state, polled as a 200
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# AC-48 — plumbing latency: POST -> task -> first ready poll under 2s with an instant fake renderer
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# AC-32 — the privacy capture: no document text, HTML, bytes or path in any log line, across a
# successful job, every failure reason, a failed inline render, a failed file read and a failed
# UPDATE. `traceback.format_exception`, never `str(exc)` (CLAUDE.md).
# ---------------------------------------------------------------------------------------------
