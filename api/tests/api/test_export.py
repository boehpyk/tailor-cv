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

import contextlib
import logging
import time
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.render_export_job import (
    RenderExportJob,
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    DocumentRenderFailed,
    DocumentRenderOutputTooLarge,
    DocumentRenderTimedOut,
)
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
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
    get_file_store,
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
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
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
        guest_session_id=GuestSessionId(guest_session_id),
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
            guest_session_id=guest_session_id,
            tailoring_run_id=TailoringRunId(UUID(run_id)),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
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
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
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


@pytest.mark.parametrize(
    "raw_body", [pytest.param(b"", id="empty"), pytest.param(b"not json at all", id="not-json")]
)
async def test_body_not_valid_json_returns_422(
    client: AsyncClient, session: AsyncSession, raw_body: bytes
) -> None:
    run_id = await _create_succeeded_run(client, session)

    response = await client.post(
        f"/api/tailoring-runs/{run_id}/exports",
        content=raw_body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"format": "pdf"}, id="document-missing"),
        pytest.param({"document": "cv"}, id="format-missing"),
        pytest.param({"document": "resume", "format": "pdf"}, id="document-unknown"),
        pytest.param({"document": "cv", "format": "md"}, id="format-inline-md"),
        pytest.param({"document": "cv", "format": "txt"}, id="format-inline-txt"),
        pytest.param(
            {"document": "cv", "format": "pdf", "extra": "nope"}, id="extra-field-forbidden"
        ),
    ],
)
async def test_malformed_body_returns_422(
    client: AsyncClient, session: AsyncSession, body: dict[str, object]
) -> None:
    run_id = await _create_succeeded_run(client, session)

    response = await client.post(f"/api/tailoring-runs/{run_id}/exports", json=body)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# X-11 — the 256 KiB JSON body cap
# ---------------------------------------------------------------------------------------------


async def test_json_body_over_the_cap_returns_413(client: AsyncClient, settings: Settings) -> None:
    padding = "a" * (settings.json_request_max_bytes + 1024)
    body = ('{"document":"cv","format":"pdf","padding":"' + padding + '"}').encode()
    assert len(body) > settings.json_request_max_bytes

    response = await client.post(
        f"/api/tailoring-runs/{uuid4()}/exports",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert _error_code(response) == "request_too_large"


# ---------------------------------------------------------------------------------------------
# X-12 — no cookie: 401, no session minted
# ---------------------------------------------------------------------------------------------


async def test_cookieless_post_returns_401_mints_no_session_and_creates_no_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = await _count_export_jobs(session)

    response = await _post_export(client, str(uuid4()))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"
    assert _guest_cookie_header(response) is None
    assert await _count_export_jobs(session) == before


# ---------------------------------------------------------------------------------------------
# X-13 — run missing or not mine: identical 404
# ---------------------------------------------------------------------------------------------


async def test_nonexistent_run_returns_404(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await _post_export(client, str(uuid4()))

    assert response.status_code == 404, response.text
    assert _error_code(response) == "tailoring_run_not_found"


async def test_run_owned_by_another_session_returns_the_same_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    async with _new_client(app) as other_client:
        foreign_run_id = await _create_succeeded_run(other_client, session)

    await _mint_cookie(client)
    response = await _post_export(client, foreign_run_id)

    assert response.status_code == 404, response.text
    assert _error_code(response) == "tailoring_run_not_found"


# ---------------------------------------------------------------------------------------------
# X-14 — the run is not succeeded: 409 tailoring_run_not_exportable, body carries status
# ---------------------------------------------------------------------------------------------


async def test_queued_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _create_queued_run(client, session)

    response = await _post_export(client, run_id)

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_exportable"
    assert response.json()["error"]["status"] == "queued"


async def test_running_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _advance_to_running(client, session)

    response = await _post_export(client, run_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["status"] == "running"


async def test_failed_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _create_failed_run(client, session)

    response = await _post_export(client, run_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["status"] == "failed"


# ---------------------------------------------------------------------------------------------
# AC-12 — the happy path's shape: 202, ExportJobResponse, Location header
# ---------------------------------------------------------------------------------------------


async def test_happy_path_returns_202_with_the_job_and_a_location_header(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)

    response = await _post_export(client, run_id, document="cv", format="pdf")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_version"] == await _current_run_version(session, run_id), (
        "AC-12: run_version equal to the run's version"
    )
    assert body["current"] is True
    assert body["file_url"] is None
    assert body["byte_size"] is None
    assert response.headers["location"] == f"/api/export-jobs/{body['id']}"


# ---------------------------------------------------------------------------------------------
# X-16 / AC-13(a) — a second identical POST while the job is in flight: 200, same id, no new row,
# no enqueue
# ---------------------------------------------------------------------------------------------


async def test_second_identical_post_returns_the_same_job_no_new_row_no_enqueue(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    queue = _install_queue(app)
    run_id = await _create_succeeded_run(client, session)

    first = await _post_export(client, run_id, document="cv", format="pdf")
    assert first.status_code == 202, first.text
    before_count = await _count_export_jobs(session)
    before_enqueued = len(queue.enqueued)

    second = await _post_export(client, run_id, document="cv", format="pdf")

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"], "X-16: the SAME job must be returned"
    assert second.headers["location"] == f"/api/export-jobs/{first.json()['id']}"
    assert await _count_export_jobs(session) == before_count, "no new row"
    assert len(queue.enqueued) == before_enqueued, "no second enqueue"


# ---------------------------------------------------------------------------------------------
# X-17 / AC-13(b),(c) — both branches: the latest job failed, or the run's version moved on
# ---------------------------------------------------------------------------------------------


async def test_a_new_job_is_created_after_the_latest_one_failed(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    first = await _post_export(client, run_id, document="cv", format="pdf")
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        first_id,
        renderer=FakeDocumentRenderer(DocumentRenderError()),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    second = await _post_export(client, run_id, document="cv", format="pdf")

    assert second.status_code == 202, second.text
    assert second.json()["id"] != first_id, "X-17: a failed job must not be handed back"


async def test_a_new_job_is_created_after_the_runs_version_moved_old_job_becomes_not_current(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    first = await _post_export(client, run_id, document="cv", format="pdf")
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    first_run_version = first.json()["run_version"]

    await _revise_cv(
        client,
        run_id,
        content=_tailored_cv_text("QA_X17_REVISED"),
        expected_version=await _current_run_version(session, run_id),
    )

    second = await _post_export(client, run_id, document="cv", format="pdf")

    assert second.status_code == 202, second.text
    assert second.json()["id"] != first_id, "X-17: a stale-version job must not be handed back"
    assert second.json()["run_version"] == await _current_run_version(session, run_id), (
        "the new job's run_version must equal the run's version after the revision"
    )
    assert second.json()["run_version"] > first_run_version, (
        "X-17: the run's version must have moved on from the first job's"
    )

    old_job = await client.get(f"/api/export-jobs/{first_id}")
    assert old_job.status_code == 200, old_job.text
    assert old_job.json()["current"] is False, "the superseded job must now read current: false"


# ---------------------------------------------------------------------------------------------
# X-18 — the session already owns the cap: 409 too_many_export_jobs, no job created
# ---------------------------------------------------------------------------------------------


async def test_session_at_the_job_cap_returns_409_and_creates_no_job(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    run_id = await _create_succeeded_run(client, session)
    guest_session_id = await _guest_session_id_for_run(session, run_id)
    await _seed_jobs_for_cap(
        session, run_id, guest_session_id, count=settings.max_export_jobs_per_session
    )
    before = await _count_export_jobs(session)

    response = await _post_export(client, run_id, document="cover_letter", format="docx")

    assert response.status_code == 409, response.text
    assert _error_code(response) == "too_many_export_jobs"
    assert await _count_export_jobs(session) == before


# ---------------------------------------------------------------------------------------------
# X-19 — rate limits: 30/hour/session, 60/hour/client-IP
# ---------------------------------------------------------------------------------------------


async def test_more_than_the_per_session_rate_limit_returns_429(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _override_settings(app, settings, export_rate_limit_per_hour=2)
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)

    first = await _post_export(client, run_id, document="cv", format="pdf")
    second = await _post_export(client, run_id, document="cv", format="docx")
    third = await _post_export(client, run_id, document="cover_letter", format="pdf")

    assert first.status_code != 429, first.text
    assert second.status_code != 429, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_per_ip_rate_limit_returns_429(
    app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _override_settings(app, settings, export_rate_limit_per_ip_per_hour=2)

    async def _attempt() -> Response:
        async with _new_client(app) as one_shot_client:
            run_id = await _create_succeeded_run(one_shot_client, session)
            _install_queue(app)
            return await _post_export(one_shot_client, run_id, document="cv", format="pdf")

    first = await _attempt()
    second = await _attempt()
    third = await _attempt()

    assert first.status_code != 429, first.text
    assert second.status_code != 429, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


# ---------------------------------------------------------------------------------------------
# X-20 — Redis unreachable: the export limiter FAILS OPEN (contrast with tailoring's fail-closed)
# ---------------------------------------------------------------------------------------------


async def test_redis_unreachable_lets_the_request_proceed(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    _override_settings(app, await _current_settings(app), redis_url="redis://127.0.0.1:1/0")
    before = await _count_export_jobs(session)

    response = await _post_export(client, run_id, document="cv", format="pdf")

    assert response.status_code in (200, 202), (
        f"X-20: the export limiter fails OPEN on an unreachable Redis — got {response.status_code}, "
        f"{response.text}"
    )
    assert await _count_export_jobs(session) == before + 1, (
        "a request that fails open must still be able to create a job"
    )


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


async def test_commit_failure_before_enqueue_returns_503_and_enqueues_nothing(
    client: AsyncClient, app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _install_queue(app)
    run_id = await _create_succeeded_run(client, session)

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (X-21)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await _post_export(client, run_id, document="cv", format="pdf")

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"
    assert queue.enqueued == [], "nothing may be enqueued when the row never committed"


# ---------------------------------------------------------------------------------------------
# X-22 — the enqueue fails after the row is committed: 503 queue_unavailable, the row survives,
# recorded failed/not_queued in a SECOND transaction
# ---------------------------------------------------------------------------------------------


async def test_broker_failure_after_commit_returns_503_and_records_failed_not_queued(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_failing_queue(app)
    run_id = await _create_succeeded_run(client, session)
    before = await _count_export_jobs(session)

    response = await _post_export(client, run_id, document="cv", format="pdf")

    assert response.status_code == 503, response.text
    assert _error_code(response) == "queue_unavailable"
    assert await _count_export_jobs(session) == before + 1, (
        "X-22: the row must survive even though the enqueue failed — this is the second-transaction "
        "assertion: a broker refusal does not roll back the already-committed row"
    )

    listing = await client.get(f"/api/tailoring-runs/{run_id}/exports")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["failure_reason"] == "not_queued"
    assert items[0]["retryable"] is True


# ---------------------------------------------------------------------------------------------
# X-41 — a malformed export-job id: 422
# ---------------------------------------------------------------------------------------------


async def test_malformed_job_id_on_poll_returns_422(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get("/api/export-jobs/not-a-uuid")

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_malformed_job_id_on_file_download_returns_422(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get("/api/export-jobs/not-a-uuid/file")

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# X-42 / X-50 — no / unknown / expired cookie on any GET: 401
# ---------------------------------------------------------------------------------------------


async def test_cookieless_poll_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"/api/export-jobs/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_cookieless_file_download_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"/api/export-jobs/{uuid4()}/file")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_unknown_cookie_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "not-a-real-token")

    response = await client.get(f"/api/export-jobs/{uuid4()}")

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------------------------
# X-43 / AC-24 — job does not exist, or belongs to another session: identical 404
# ---------------------------------------------------------------------------------------------


async def test_nonexistent_job_returns_404(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(f"/api/export-jobs/{uuid4()}")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "export_job_not_found"


async def test_job_owned_by_another_session_returns_the_same_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    async with _new_client(app) as other_client:
        _install_queue(app)
        foreign_run_id = await _create_succeeded_run(other_client, session)
        create = await _post_export(other_client, foreign_run_id, document="cv", format="pdf")
        assert create.status_code == 202, create.text
        foreign_job_id = create.json()["id"]

    await _mint_cookie(client)
    response = await client.get(f"/api/export-jobs/{foreign_job_id}")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "export_job_not_found"


async def test_nonexistent_job_file_download_returns_404(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(f"/api/export-jobs/{uuid4()}/file")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "export_job_not_found"


# ---------------------------------------------------------------------------------------------
# X-44 / X-45 / AC-26 — the file route on a queued/rendering/failed job: 409 export_not_ready
# ---------------------------------------------------------------------------------------------


async def test_file_download_on_a_queued_job_returns_409_export_not_ready(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 409, response.text
    assert _error_code(response) == "export_not_ready"
    assert response.json()["error"]["status"] == "queued"


async def test_file_download_on_a_rendering_job_returns_409_export_not_ready(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]
    # A renderer that sleeps longer than this test needs to observe `rendering` mid-flight would be
    # slow and flaky; instead, mark the job `rendering` directly through the repository, matching
    # what `RenderExportJob` step 4 does before it ever calls the renderer.
    repo = _export_repo(session)
    job = await repo.get(ExportJobId(UUID(job_id)))  # type: ignore[attr-defined]
    job.mark_started(_now_whole_second())
    await repo.save(job)  # type: ignore[attr-defined]
    await session.commit()

    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["status"] == "rendering"


async def test_file_download_on_a_failed_job_returns_409_with_the_failure_reason(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]
    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(DocumentRenderError()),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["status"] == "failed"
    assert response.json()["error"]["failure_reason"] == "render_error"


# ---------------------------------------------------------------------------------------------
# X-46 — a stale ready job's file is still served
# ---------------------------------------------------------------------------------------------


async def test_file_download_on_a_stale_ready_job_still_serves_the_file(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]
    files = InMemoryFileStore()
    outcome = await _run_export_worker(
        session, settings, job_id, renderer=FakeDocumentRenderer(b"%PDF-fake-bytes"), files=files
    )
    assert outcome is RenderExportJobOutcome.READY

    await _revise_cv(
        client,
        run_id,
        content=_tailored_cv_text("QA_X46_REVISED"),
        expected_version=await _current_run_version(session, run_id),
    )

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["current"] is False, "the job must now read stale"

    app.dependency_overrides[get_file_store] = lambda: files
    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 200, response.text
    assert response.content == b"%PDF-fake-bytes"


# ---------------------------------------------------------------------------------------------
# X-47 / AC-26 — the file is missing from the store on a ready job: 410 export_file_gone
# ---------------------------------------------------------------------------------------------


async def test_file_missing_from_store_returns_410_export_file_gone(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]
    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(b"bytes"),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.READY

    app.dependency_overrides[get_file_store] = lambda: MissingFileStore()
    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 410, response.text
    assert _error_code(response) == "export_file_gone"


# ---------------------------------------------------------------------------------------------
# X-48 / AC-26 — the store is unreadable: 503 storage_unavailable (corrected from
# `service_unavailable` at I16 — the existing FileStoreUnavailable branch already maps this way)
# ---------------------------------------------------------------------------------------------


async def test_store_unreadable_returns_503_storage_unavailable(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]
    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(b"bytes"),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.READY

    app.dependency_overrides[get_file_store] = lambda: AlwaysFailingFileStore()
    response = await client.get(f"/api/export-jobs/{job_id}/file")

    assert response.status_code == 503, response.text
    assert _error_code(response) == "storage_unavailable"


# ---------------------------------------------------------------------------------------------
# AC-17 — the whole path with the fake renderer and the in-memory file store
# ---------------------------------------------------------------------------------------------


async def test_the_whole_queued_path_yields_a_ready_job_with_a_downloadable_file(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    files = InMemoryFileStore()
    fake_renderer = FakeDocumentRenderer(b"%PDF-1.4 fake rendered bytes")
    outcome = await _run_export_worker(
        session, settings, job_id, renderer=fake_renderer, files=files
    )
    assert outcome is RenderExportJobOutcome.READY
    assert len(fake_renderer.calls) == 1

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    body = poll.json()
    assert body["status"] == "ready"
    assert body["byte_size"] == len(b"%PDF-1.4 fake rendered bytes")
    assert body["render_duration_ms"] >= 0
    assert body["completed_at"] is not None
    assert body["file_url"] == f"/api/export-jobs/{job_id}/file"
    assert body["failure_reason"] is None

    app.dependency_overrides[get_file_store] = lambda: files
    download = await client.get(f"/api/export-jobs/{job_id}/file")
    assert download.status_code == 200, download.text
    assert download.content == b"%PDF-1.4 fake rendered bytes"
    assert download.headers.get("content-type") == "application/pdf"
    assert download.headers.get("content-length") == str(len(download.content))
    assert download.headers.get("content-disposition") == 'attachment; filename="tailored-cv.pdf"'
    assert download.headers.get("cache-control") == "no-store"
    assert download.headers.get("x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------------------------
# AC-19 — every failure reason the worker can produce is a recorded state, polled as a 200
# ---------------------------------------------------------------------------------------------


async def test_render_failed_reason_is_polled_as_200_with_that_reason(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(DocumentRenderError()),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["status"] == "failed"
    assert poll.json()["failure_reason"] == "render_error"


async def test_render_timed_out_reason_is_polled_as_200_with_that_reason(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(DocumentRenderTimedOut()),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["failure_reason"] == "render_timed_out"


async def test_output_too_large_reason_is_polled_as_200_with_that_reason(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(DocumentRenderOutputTooLarge()),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["failure_reason"] == "output_too_large"


async def test_file_store_unavailable_reason_is_polled_as_200_with_that_reason(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(b"bytes that never reach a working store"),
        files=AlwaysFailingFileStore(),
    )
    assert outcome is RenderExportJobOutcome.FAILED

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["failure_reason"] == "file_store_unavailable"


async def test_source_changed_reason_is_polled_as_200_and_the_renderer_is_never_called(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    await _revise_cv(
        client,
        run_id,
        content=_tailored_cv_text("QA_SOURCE_CHANGED"),
        expected_version=await _current_run_version(session, run_id),
    )

    renderer = FakeDocumentRenderer(b"must never be produced")
    outcome = await _run_export_worker(
        session, settings, job_id, renderer=renderer, files=InMemoryFileStore()
    )
    assert outcome is RenderExportJobOutcome.FAILED
    assert renderer.calls == [], "X-31: the version check runs BEFORE the render"

    poll = await client.get(f"/api/export-jobs/{job_id}")
    assert poll.status_code == 200, poll.text
    assert poll.json()["failure_reason"] == "source_changed"


# ---------------------------------------------------------------------------------------------
# AC-48 — plumbing latency: POST -> task -> first ready poll under 2s with an instant fake renderer
# ---------------------------------------------------------------------------------------------


@pytest.mark.slow
async def test_post_to_ready_poll_completes_under_two_seconds_with_an_instant_fake_renderer(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    _install_queue(app)
    run_id = await _create_succeeded_run(client, session)

    started = time.perf_counter()
    create = await _post_export(client, run_id, document="cv", format="pdf")
    assert create.status_code == 202, create.text
    job_id = create.json()["id"]

    outcome = await _run_export_worker(
        session,
        settings,
        job_id,
        renderer=FakeDocumentRenderer(b"instant bytes"),
        files=InMemoryFileStore(),
    )
    assert outcome is RenderExportJobOutcome.READY

    poll = await client.get(f"/api/export-jobs/{job_id}")
    elapsed = time.perf_counter() - started

    assert poll.status_code == 200, poll.text
    assert poll.json()["status"] == "ready"
    assert elapsed < 2.0, (
        f"POST -> ready poll took {elapsed:.3f}s (AC-48 plumbing budget: under 2s)"
    )


# ---------------------------------------------------------------------------------------------
# AC-32 — the privacy capture: no document text, HTML, bytes or path in any log line, across a
# successful job, every failure reason, a failed inline render, a failed file read and a failed
# UPDATE. `traceback.format_exception`, never `str(exc)` (CLAUDE.md).
# ---------------------------------------------------------------------------------------------


async def test_privacy_no_document_text_or_bytes_leak_across_success_every_failure_a_failed_read_and_a_failed_write(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    success_marker = "QA_PRIVACY_EXPORT_SUCCESS_TOKEN"
    render_failed_marker = "QA_PRIVACY_RENDER_FAILED_TOKEN"
    timed_out_marker = "QA_PRIVACY_TIMED_OUT_TOKEN"
    store_unavailable_marker = "QA_PRIVACY_STORE_UNAVAILABLE_TOKEN"
    inline_fail_marker = "QA_PRIVACY_INLINE_FAIL_TOKEN"
    write_fail_marker = "QA_PRIVACY_WRITE_FAIL_TOKEN"
    rendered_bytes_marker = b"QA_PRIVACY_RENDERED_BYTES_TOKEN"

    # This test calls `_create_succeeded_run` six times under ONE guest session (one success, two
    # failure-reason runs, one store-unavailable run, one inline-failure run, one write-failure run)
    # — each uploads a real base CV through `POST /api/base-cvs` (`_advance_to_running`'s own
    # technique). `max_base_cvs_per_session` defaults to 5, so the sixth upload answered 409
    # `too_many_base_cvs` and the fixture died at `_upload_ready_base_cv`'s own `assert 201` before
    # this test's actual subject — the privacy assertions below — was ever reached. Postings (six
    # against a cap of 10) and the upload/posting rate limits (six against 10/hr and 20/hr) all stay
    # under their own caps unmodified; only this one cap needs raising, using the same
    # `_override_settings` + `model_copy` pattern `test_export_inline.py`'s AC-11 test already
    # establishes for an unrelated cap. No assertion below changes — the setup was broken, not the
    # thing it exists to prove.
    _override_settings(app, settings, max_base_cvs_per_session=10)

    _install_queue(app)

    with caplog.at_level(logging.INFO):
        # --- a full successful job ---
        run_id = await _create_succeeded_run(client, session, cv_marker=success_marker)
        create = await _post_export(client, run_id, document="cv", format="pdf")
        assert create.status_code == 202, create.text
        job_id = create.json()["id"]
        files = InMemoryFileStore()
        outcome = await _run_export_worker(
            session,
            settings,
            job_id,
            renderer=FakeDocumentRenderer(rendered_bytes_marker),
            files=files,
        )
        assert outcome is RenderExportJobOutcome.READY

        # --- every failure reason the worker can record ---
        for marker, exc in (
            (render_failed_marker, DocumentRenderError()),
            (timed_out_marker, DocumentRenderTimedOut()),
        ):
            fail_run_id = await _create_succeeded_run(client, session, cv_marker=marker)
            fail_create = await _post_export(client, fail_run_id, document="cv", format="pdf")
            assert fail_create.status_code == 202, fail_create.text
            fail_job_id = fail_create.json()["id"]
            fail_outcome = await _run_export_worker(
                session,
                settings,
                fail_job_id,
                renderer=FakeDocumentRenderer(exc),
                files=InMemoryFileStore(),
            )
            assert fail_outcome is RenderExportJobOutcome.FAILED

        store_run_id = await _create_succeeded_run(
            client, session, cv_marker=store_unavailable_marker
        )
        store_create = await _post_export(client, store_run_id, document="cv", format="pdf")
        assert store_create.status_code == 202, store_create.text
        store_job_id = store_create.json()["id"]
        store_outcome = await _run_export_worker(
            session,
            settings,
            store_job_id,
            renderer=FakeDocumentRenderer(b"irrelevant bytes"),
            files=AlwaysFailingFileStore(),
        )
        assert store_outcome is RenderExportJobOutcome.FAILED

        # --- a failed inline render (X-5) ---
        inline_run_id = await _create_succeeded_run(client, session, cv_marker=inline_fail_marker)
        app.dependency_overrides[get_document_renderer] = lambda: FakeDocumentRenderer(
            DocumentRenderError()
        )
        inline_response = await client.get(
            f"/api/tailoring-runs/{inline_run_id}/documents/cv/download?format=txt"
        )
        assert inline_response.status_code == 500, inline_response.text
        del app.dependency_overrides[get_document_renderer]

        # --- a failed file read (X-47/X-48) ---
        app.dependency_overrides[get_file_store] = lambda: AlwaysFailingFileStore()
        read_fail_response = await client.get(f"/api/export-jobs/{job_id}/file")
        assert read_fail_response.status_code == 503, read_fail_response.text
        del app.dependency_overrides[get_file_store]

        # --- a failed UPDATE (the commit fails while recording an outcome) ---
        write_fail_run_id = await _create_succeeded_run(
            client, session, cv_marker=write_fail_marker
        )
        write_fail_create = await _post_export(
            client, write_fail_run_id, document="cv", format="pdf"
        )
        assert write_fail_create.status_code == 202, write_fail_create.text
        write_fail_job_id = write_fail_create.json()["id"]

        original_commit = session.commit

        async def _raise_with_traceback() -> None:
            try:
                raise SQLAlchemyError(
                    f"simulated failing UPDATE carrying {write_fail_marker} in its parameters"
                )
            except SQLAlchemyError as exc:
                logging.getLogger("tests.export.privacy").info(
                    "captured_traceback_for_assertion_only",
                    extra={"formatted": "".join(traceback.format_exception(exc))},
                )
                raise

        monkeypatch.setattr(session, "commit", _raise_with_traceback)
        with contextlib.suppress(SQLAlchemyError):
            await _run_export_worker(
                session,
                settings,
                write_fail_job_id,
                renderer=FakeDocumentRenderer(b"bytes for the write-failure case"),
                files=InMemoryFileStore(),
            )
        monkeypatch.setattr(session, "commit", original_commit)

    log_output = caplog.text

    assert "domain_event" in log_output, (
        "expected at least one domain_event line — the positive proof that the export channel was "
        "captured at all, or the marker-absence assertions below would pass vacuously"
    )

    for marker in (
        success_marker,
        render_failed_marker,
        timed_out_marker,
        store_unavailable_marker,
        inline_fail_marker,
        write_fail_marker,
    ):
        assert marker not in log_output, f"{marker!r} leaked into the logs"
    assert rendered_bytes_marker.decode() not in log_output, "rendered bytes leaked into the logs"


@pytest.mark.slow
def test_privacy_weasyprint_and_fonttools_never_reach_a_handler_and_the_typed_url_never_leaks(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The assertion added at I15, carried into I17: `weasyprint` logs the user's own typed URL at
    ERROR on a hostile fixture — a privacy control, not noise control — so this must be captured
    **after** `configure_logging(settings)` has run, never via `caplog.at_level()` on the root
    logger alone (that would prove nothing about the vendor logger's own effective level, which is
    where the silencing actually happens)."""
    from tailorcraft.infrastructure.export.pdf import render_pdf
    from tailorcraft.infrastructure.observability import configure_logging

    configure_logging(settings)

    hostile_html = (
        "<html><body><p>Jane Doe</p>"
        '<link rel="stylesheet" href="http://jane-doe-private.example/x.css">'
        '<img src="http://jane-doe-private.example/photo.png">'
        "</body></html>"
    )

    with caplog.at_level(logging.DEBUG):
        render_pdf(hostile_html)

    for record in caplog.records:
        assert not record.name.startswith("weasyprint"), (
            f"a weasyprint record reached a handler: {record.name} {record.getMessage()!r}"
        )
        assert not record.name.startswith("fontTools"), (
            f"a fontTools record reached a handler: {record.name}"
        )
    assert "jane-doe-private.example" not in caplog.text, (
        "the user's typed URL must never appear in a captured log line (X-54)"
    )
