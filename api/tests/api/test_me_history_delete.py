"""API tests for `DELETE /api/me/tailoring-runs/{id}` — deleting a history entry (slice 2.3, T21 RED;
AC-33, H-41, H-42, H-44…H-48, H-51).

The run, its export jobs and — iff no other run references it — its posting are deleted **and
committed** before any file is unlinked; the files are gone afterwards; an entry in progress is 409
with nothing touched; a second or concurrent delete is 404 for the loser, which unlinks nothing.

- **"Committed before unlink"** is observed from a **separate connection** at the moment the file
  store's `delete` is called, on `concurrent_app` (a real session per request) — 2.2's AC-27
  technique; on the shared `app` fixture every statement is visible on the one connection whether or
  not a commit ran. Those tests write real, committed rows and delete their user at teardown.
- **Faults below the floor**: the request's own `session.commit` (H-46); a **directory planted at
  the export key** so the real `unlink` fails (H-48) — never a patched adapter method.
- **H-47 is proven by composition** (2.2's S-44 shape): the order is the committed-before-unlink
  test below plus `EraseHistoryEntry`'s order double (T10); the survivor — rows gone, file left — is
  constructed with the real `SqlAlchemyHistoryEntryData` and reclaimed by the real sweep. That half
  runs on landed code and is green on arrival; the "a retry is 404" half is red.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.api.me_support import (
    ME_BASE_CVS,
    ME_RUNS,
    Account,
    Entry,
    assert_test_database,
    count_rows,
    error_body,
    error_code,
    new_client,
    register,
    seed_entry,
)
from tests.integration.owners import queued_run, ready_export, running_run, succeeded_run


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """A real session per request (`main.py`'s production wiring), for the commit-visibility and
    race tests — 2.2's `concurrent_app`, reproduced for the same reason."""
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = password_hasher
    return app


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Registration's limiter lives in Redis."""


def _path(settings: Settings, ref: FileRef) -> Path:
    return settings.upload_dir / ref.key


async def _entry(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> tuple[Account, Entry]:
    account = await register(client, settings)
    entry = await seed_entry(
        session,
        settings,
        account.owner,
        at=clock.now() - timedelta(minutes=10),
        ready_formats=(ExportFormat.PDF, ExportFormat.DOCX),
    )
    return account, entry


# --- H-41: the happy path -----------------------------------------------------------------------


async def test_h41_delete_is_204_and_takes_the_run_its_jobs_its_posting_and_its_files(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    paths = [_path(settings, job.storage_ref) for job in entry.jobs]
    assert all(path.exists() for path in paths)

    with caplog.at_level(logging.INFO):
        response = await client.delete(entry.run_url, headers=account.headers)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 0
    assert await count_rows(session, "export_job", tailoring_run_id=entry.run_id.value) == 0
    assert await count_rows(session, "posting_job_posting", id=entry.posting_id.value) == 0
    assert not any(path.exists() for path in paths)
    assert await count_rows(session, "intake_base_cv", id=entry.cv_id.value) == 1, (
        "the saved CV is not part of the entry"
    )
    lines = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "retention.history_entry_erased" in r.getMessage()
    ]
    assert len(lines) == 1
    assert lines[0]["user_id"] == str(account.user_id.value)
    assert lines[0]["tailoring_run_id"] == str(entry.run_id.value)
    assert (lines[0]["export_jobs"], lines[0]["files_unlinked"], lines[0]["files_failed"]) == (
        2,
        2,
        0,
    )
    assert lines[0]["posting_deleted"] is True


async def test_h45_a_posting_shared_with_another_entry_survives(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)
    sibling = succeeded_run(account.owner, clock.now(), job_posting_id=entry.posting_id)
    await SqlAlchemyTailoringRunRepository(session).add(sibling)
    await session.flush()

    response = await client.delete(entry.run_url, headers=account.headers)

    assert response.status_code == 204, response.text
    assert await count_rows(session, "posting_job_posting", id=entry.posting_id.value) == 1
    assert await count_rows(session, "tailoring_run", id=sibling.id.value) == 1


# --- H-42: in progress --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "status"),
    [(queued_run, "queued"), (running_run, "running")],
    ids=["queued", "running"],
)
async def test_h42_an_entry_in_progress_is_409_with_nothing_touched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    build: Callable[..., TailoringRun],
    status: str,
) -> None:
    account = await register(client, settings)
    run = build(account.owner, clock.now())
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now(), run=run, ready_formats=()
    )

    response = await client.delete(entry.run_url, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_in_progress"
    assert error_body(response)["status"] == status
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 1
    assert await count_rows(session, "posting_job_posting", id=entry.posting_id.value) == 1


# --- H-44: a second delete ----------------------------------------------------------------------


async def test_h44_a_second_delete_is_404(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)

    first = await client.delete(entry.run_url, headers=account.headers)
    second = await client.delete(entry.run_url, headers=account.headers)

    assert first.status_code == 204, first.text
    assert second.status_code == 404, second.text
    assert error_code(second) == "tailoring_run_not_found"


# --- H-46: the commit fails ---------------------------------------------------------------------


async def test_h46_a_failed_commit_is_503_with_nothing_deleted_and_no_file_touched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, entry = await _entry(client, settings, session, clock)

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (H-46)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.delete(entry.run_url, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 1
    assert all(_path(settings, job.storage_ref).exists() for job in entry.jobs)


# --- H-48: an unlink fails ----------------------------------------------------------------------


async def test_h48_a_failing_unlink_is_still_204_and_logs_one_warning_per_failure(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A directory at the export key: the real `unlink` fails (`IsADirectoryError`), below every
    adapter floor. The rows are already gone and committed, so the answer is still 204."""
    account, entry = await _entry(client, settings, session, clock)
    blocked = _path(settings, entry.jobs[0].storage_ref)
    blocked.unlink()
    blocked.mkdir()
    (blocked / "keep").write_bytes(b"x")

    with caplog.at_level(logging.WARNING):
        response = await client.delete(entry.run_url, headers=account.headers)

    assert response.status_code == 204, response.text
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 0
    assert not _path(settings, entry.jobs[1].storage_ref).exists()
    warnings = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "retention.history_entry_file_unlink_failed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0]["tailoring_run_id"] == str(entry.run_id.value)
    assert warnings[0]["error_type"]
    assert str(blocked) not in caplog.text
    assert entry.jobs[0].storage_ref.key not in caplog.text


# --- H-47: the crash between commit and unlink, by composition --------------------------------


async def test_h47_the_crash_survivor_is_reclaimed_by_the_real_sweep_and_a_retry_is_404(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=clock.now(), ready_formats=())
    files = LocalFileStore(tmp_path)
    ref = FileRef.for_export((await _job_on(session, account, entry, clock)).id, ExportFormat.PDF)
    await files.put(ref, b"rendered, then orphaned by a crash")

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        account.user_id, entry.run_id.value
    )  # the rows half committed; the process "dies" before any unlink
    assert deleted is not None
    assert ref in deleted.export_files
    old = (clock.now() - timedelta(hours=60)).timestamp()
    os.utime(tmp_path / ref.key, (old, old))
    report = await ReclaimOrphanedFiles(
        LocalOrphanFileScanner(tmp_path),
        SqlAlchemyExpiredGuestData(session),
        files,
        clock,
        RetentionWindow(hours=24),
        timedelta(hours=24),
    )()

    assert report.reclaimed == 1
    assert not (tmp_path / ref.key).exists()
    retry = await client.delete(entry.run_url, headers=account.headers)
    assert retry.status_code == 404, retry.text
    assert error_code(retry) == "tailoring_run_not_found"


async def _job_on(
    session: AsyncSession, account: Account, entry: Entry, clock: FixedClock
) -> ExportJob:
    job = ready_export(account.owner, entry.run, clock.now(), format=ExportFormat.PDF)
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.flush()
    return job


# --- H-51: a saved CV deleted while entries reference it ---------------------------------------


async def test_h51_deleting_a_saved_cv_leaves_its_entries_readable(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account, entry = await _entry(client, settings, session, clock)

    removed = await client.delete(f"{ME_BASE_CVS}/{entry.cv_id.value}", headers=account.headers)
    reopened = await client.get(entry.run_url, headers=account.headers)
    history = await client.get(ME_RUNS, headers=account.headers)

    assert removed.status_code == 204, removed.text
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["tailored_cv"] is not None
    assert history.status_code == 200, history.text
    (item,) = history.json()["items"]
    assert item["base_cv"] is None
    assert item["base_cv_id"] == str(entry.cv_id.value)


# --- AC-33 on real commits: committed before unlink; two concurrent deletes -------------------


async def _seed_committed(
    app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> tuple[Account, Entry]:
    assert_test_database(settings)
    async with new_client(app) as setup:
        account = await register(setup, settings)
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        entry = await seed_entry(
            seeding, settings, account.owner, at=clock.now(), ready_formats=(ExportFormat.PDF,)
        )
        await seeding.commit()
    return account, entry


async def _drop_user(engine: AsyncEngine, account: Account) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM identity_user WHERE id = :id"), {"id": account.user_id.value}
        )


async def test_ac33_the_rows_are_committed_before_any_file_is_unlinked(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    account, entry = await _seed_committed(concurrent_app, settings, engine, clock)
    run_present_at_unlink: list[bool] = []
    original_delete = LocalFileStore.delete

    async def _checking_delete(self: LocalFileStore, ref: FileRef) -> None:
        async with engine.connect() as separate:
            found = await separate.execute(
                text("SELECT count(*) FROM tailoring_run WHERE id = :id"),
                {"id": entry.run_id.value},
            )
            run_present_at_unlink.append(found.scalar_one() > 0)
        await original_delete(self, ref)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(LocalFileStore, "delete", _checking_delete)
            async with new_client(concurrent_app) as deleter:
                response = await deleter.delete(entry.run_url, headers=account.headers)

        assert response.status_code == 204, response.text
        assert run_present_at_unlink == [False], (
            "the run must already be committed-gone, on a separate connection, when the file is "
            "unlinked"
        )
    finally:
        await _drop_user(engine, account)


async def test_ac33_two_concurrent_deletes_are_one_204_and_one_404(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    account, entry = await _seed_committed(concurrent_app, settings, engine, clock)
    try:
        async with new_client(concurrent_app) as one, new_client(concurrent_app) as two:
            results = await asyncio.gather(
                one.delete(entry.run_url, headers=account.headers),
                two.delete(entry.run_url, headers=account.headers),
            )

        assert sorted(r.status_code for r in results) == [204, 404], [r.text for r in results]
        loser = next(r for r in results if r.status_code == 404)
        assert error_code(loser) == "tailoring_run_not_found"
        assert not (settings.upload_dir / entry.jobs[0].storage_ref.key).exists()
    finally:
        await _drop_user(engine, account)
