"""API tests for re-export on a user-owned run (slice 2.3, T21 RED; AC-32, H-7, H-36…H-40).

`GET …/documents/{kind}/download?format=md|txt` (inline, no row), `POST …/exports` (202 new / 200
existing), `GET …/exports`, `GET /api/me/export-jobs/{id}` (poll; `current` computed as 1.5) and
`GET /api/me/export-jobs/{id}/file` (bytes; 409 `export_not_ready`; 410 `export_file_gone`). Every
handler is a T20 skeleton — each test is red on its first status assertion. Runs and jobs are
seeded through the real repositories; files sit on the real upload volume at each job's derived key.
The export broker is a double on `get_export_queue`; the worker (`RenderExportJob`) is unedited in
2.3, so H-7/H-40 are asserted on the rows it leaves behind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.errors import ExportNotQueued
from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat, ExportJobId
from tailorcraft.domain.tailoring.value_objects import TailoredCv
from tailorcraft.infrastructure.api.deps import get_export_queue
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    EXPORT_JOB_RESPONSE_KEYS,
    ME_EXPORT_JOBS,
    ME_RUNS,
    Account,
    Entry,
    count_rows,
    error_body,
    error_code,
    export_bytes,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.integration.fakes import FakeExportQueue
from tests.integration.owners import queued_export, queued_run

_REVISED_CV = "Revised curriculum vitae line. " * 30


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The export limiter lives in Redis, which the rollback never reaches."""


def _install_queue(app: FastAPI, *, refuse: bool = False) -> FakeExportQueue:
    queue = FakeExportQueue(outcome=ExportNotQueued("simulated broker refusal") if refuse else None)
    app.dependency_overrides[get_export_queue] = lambda: queue
    return queue


async def _entry(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> tuple[Account, Entry]:
    account = await register(client, settings)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now() - timedelta(minutes=10)
    )
    return account, entry


def _exports_url(entry: Entry) -> str:
    return f"{ME_RUNS}/{entry.run_id.value}/exports"


# --- Inline download ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [("md", "text/markdown; charset=utf-8"), ("txt", "text/plain; charset=utf-8")],
)
async def test_inline_download_serves_the_current_document_with_1_5s_headers_and_no_row(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    fmt: str,
    content_type: str,
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    run = entry.run
    run.revise_cv(TailoredCv(_REVISED_CV), expected_version=run.version, at=clock.now())
    await SqlAlchemyTailoringRunRepository(session).save(run)
    await session.flush()
    jobs_before = await count_rows(session, "export_job", tailoring_run_id=run.id.value)

    response = await client.get(
        f"{ME_RUNS}/{run.id.value}/documents/cv/download?format={fmt}", headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.headers.get("content-type") == content_type
    assert (
        response.headers.get("content-disposition") == f'attachment; filename="tailored-cv.{fmt}"'
    )
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert "Revised curriculum vitae line." in response.text
    assert await count_rows(session, "export_job", tailoring_run_id=run.id.value) == jobs_before


async def test_inline_download_of_a_queued_run_is_409_not_exportable(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = queued_run(account.owner, clock.now())
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.flush()

    response = await client.get(
        f"{ME_RUNS}/{run.id.value}/documents/cv/download?format=md", headers=account.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_not_exportable"
    assert error_body(response)["status"] == "queued"


# --- POST …/exports -----------------------------------------------------------------------------


async def test_a_new_export_is_202_queued_user_owned_with_location(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    queue = _install_queue(app)
    account, entry = await _entry(client, settings, session, clock)

    response = await client.post(
        _exports_url(entry),
        json={"document": "cover_letter", "format": "docx"},
        headers=account.headers,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == EXPORT_JOB_RESPONSE_KEYS
    assert body["status"] == "queued"
    assert body["expires_at"] is None
    assert body["file_url"] is None
    assert response.headers["location"] == f"{ME_EXPORT_JOBS}/{body['id']}"
    assert queue.enqueued == [ExportJobId(UUID(body["id"]))]
    assert (
        await count_rows(session, "export_job", id=UUID(body["id"]), user_id=account.user_id.value)
        == 1
    )


async def test_h38_the_same_key_and_version_is_200_with_the_existing_job(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    queue = _install_queue(app)
    account, entry = await _entry(client, settings, session, clock)
    existing = entry.jobs[0]  # the seeded ready CV/PDF job at the run's current version

    response = await client.post(
        _exports_url(entry), json={"document": "cv", "format": "pdf"}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(existing.id.value)
    assert response.headers["location"] == f"{ME_EXPORT_JOBS}/{existing.id.value}"
    assert queue.enqueued == []
    assert await count_rows(session, "export_job", tailoring_run_id=entry.run_id.value) == 1


async def test_h37_at_twenty_per_run_the_request_is_409_with_no_row(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_export_jobs_per_user_run=2)
    queue = _install_queue(app)
    account, entry = await _entry(client, settings, session, clock)  # one job already
    await SqlAlchemyExportJobRepository(session).add(
        queued_export(account.owner, entry.run, clock.now(), format=ExportFormat.DOCX)
    )
    await session.flush()

    response = await client.post(
        _exports_url(entry),
        json={"document": "cover_letter", "format": "pdf"},
        headers=account.headers,
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "too_many_export_jobs"
    assert "many times" in str(error_body(response)["message"])
    assert queue.enqueued == []
    assert await count_rows(session, "export_job", tailoring_run_id=entry.run_id.value) == 2


async def test_h37_the_cap_is_per_run(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_export_jobs_per_user_run=1)
    _install_queue(app)
    account, full = await _entry(client, settings, session, clock)  # one job: at the cap
    fresh = await seed_entry(session, settings, account.owner, at=clock.now(), ready_formats=())

    response = await client.post(
        _exports_url(fresh), json={"document": "cv", "format": "pdf"}, headers=account.headers
    )

    assert response.status_code == 202, response.text
    assert full.jobs


async def test_a_refused_publish_is_503_queue_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _install_queue(app, refuse=True)
    account, entry = await _entry(client, settings, session, clock)

    response = await client.post(
        _exports_url(entry),
        json={"document": "cover_letter", "format": "pdf"},
        headers=account.headers,
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "queue_unavailable"


async def test_exporting_a_queued_run_is_409_not_exportable(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _install_queue(app)
    account = await register(client, settings)
    run = queued_run(account.owner, clock.now())
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.flush()

    response = await client.post(
        f"{ME_RUNS}/{run.id.value}/exports",
        json={"document": "cv", "format": "pdf"},
        headers=account.headers,
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_not_exportable"


async def test_the_export_limiter_is_per_user_and_fails_open(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, export_rate_limit_per_hour=1)
    _install_queue(app)
    alice, alice_entry = await _entry(client, settings, session, clock)
    bob, bob_entry = await _entry(client, settings, session, clock)

    first = await client.post(
        _exports_url(alice_entry),
        json={"document": "cover_letter", "format": "pdf"},
        headers=alice.headers,
    )
    second = await client.post(
        _exports_url(alice_entry),
        json={"document": "cover_letter", "format": "docx"},
        headers=alice.headers,
    )
    other = await client.post(
        _exports_url(bob_entry),
        json={"document": "cover_letter", "format": "pdf"},
        headers=bob.headers,
    )
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")
    redis_down = await client.post(
        _exports_url(bob_entry),
        json={"document": "cover_letter", "format": "docx"},
        headers=bob.headers,
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert other.status_code == 202, other.text
    assert redis_down.status_code == 202, redis_down.text


# --- GET …/exports and the job ------------------------------------------------------------------


async def test_the_runs_export_list_holds_only_its_jobs(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    await seed_entry(session, settings, account.owner, at=clock.now())  # another run, another job

    response = await client.get(_exports_url(entry), headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items"}
    assert [item["id"] for item in body["items"]] == [str(entry.jobs[0].id.value)]
    assert set(body["items"][0]) == EXPORT_JOB_RESPONSE_KEYS


async def test_a_ready_job_is_current_with_a_user_file_url_until_the_run_is_edited(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = entry.jobs[0]
    url = f"{ME_EXPORT_JOBS}/{job.id.value}"

    before = await client.get(url, headers=account.headers)

    assert before.status_code == 200, before.text
    body = before.json()
    assert set(body) == EXPORT_JOB_RESPONSE_KEYS
    assert body["status"] == "ready"
    assert body["current"] is True
    assert body["expires_at"] is None
    assert body["file_url"] == f"{ME_EXPORT_JOBS}/{job.id.value}/file"
    assert before.headers.get("cache-control") == "no-store"

    entry.run.revise_cv(TailoredCv(_REVISED_CV), expected_version=entry.run_version, at=clock.now())
    await SqlAlchemyTailoringRunRepository(session).save(entry.run)
    await session.flush()
    after = await client.get(url, headers=account.headers)

    assert after.status_code == 200, after.text
    assert after.json()["current"] is False


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [(ExportFailureReason.RENDER_FAILED, False), (ExportFailureReason.RENDER_TIMED_OUT, True)],
)
async def test_h7_a_failed_render_reads_as_1_5_on_the_user_route(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    reason: ExportFailureReason,
    retryable: bool,
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = queued_export(account.owner, entry.run, clock.now(), format=ExportFormat.DOCX)
    job.mark_started(clock.now())
    job.mark_failed(reason, clock.now())
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.flush()

    response = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}", headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["failure_reason"] == reason.value
    assert response.json()["retryable"] is retryable
    assert response.json()["file_url"] is None


async def test_h40_a_job_no_worker_picked_up_stays_queued(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = queued_export(account.owner, entry.run, clock.now(), format=ExportFormat.DOCX)
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.flush()

    response = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}", headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"


# --- The file -----------------------------------------------------------------------------------


async def test_a_ready_file_is_served_with_1_5s_headers(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = entry.jobs[0]

    response = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}/file", headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.content == export_bytes(job.id)
    assert response.headers.get("content-type") == "application/pdf"
    assert response.headers.get("content-disposition") == 'attachment; filename="tailored-cv.pdf"'
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"


async def test_a_file_that_is_not_ready_is_409_export_not_ready(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = queued_export(account.owner, entry.run, clock.now(), format=ExportFormat.DOCX)
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.flush()

    response = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}/file", headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "export_not_ready"
    assert error_body(response)["status"] == "queued"


async def test_h39_a_ready_row_whose_file_is_gone_is_410_and_the_row_is_kept(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    job = entry.jobs[0]
    (settings.upload_dir / job.storage_ref.key).unlink()

    response = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}/file", headers=account.headers)

    assert response.status_code == 410, response.text
    assert error_code(response) == "export_file_gone"
    assert await count_rows(session, "export_job", id=job.id.value) == 1


async def test_h36_another_users_job_is_404_export_job_not_found(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _, entry = await _entry(client, settings, session, clock)
    intruder = await register(client, settings)

    poll = await client.get(f"{ME_EXPORT_JOBS}/{entry.jobs[0].id.value}", headers=intruder.headers)
    file = await client.get(
        f"{ME_EXPORT_JOBS}/{entry.jobs[0].id.value}/file", headers=intruder.headers
    )

    assert poll.status_code == 404, poll.text
    assert error_code(poll) == "export_job_not_found"
    assert file.status_code == 404, file.text
    assert error_code(file) == "export_job_not_found"
