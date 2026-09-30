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
import contextvars
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.retention.value_objects import DeletedHistoryEntry, RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.api.deps import get_tailoring_queue
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
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
from tests.api.me_support import (
    ME_BASE_CVS,
    ME_RUNS,
    Account,
    Entry,
    assert_test_database,
    build_concurrent_app,
    count_rows,
    error_body,
    error_code,
    new_client,
    register,
    seed_entry,
)
from tests.integration.fakes import FakeTailoringQueue
from tests.integration.owners import queued_run, ready_export, running_run, succeeded_run


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """A real session per request, for the commit-visibility and race tests (`me_support`)."""
    return build_concurrent_app(settings, engine, password_hasher)


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
    await session.commit()

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
    await session.commit()
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
    """AC-33/H-44, hardened (reviewer /verify round-1 MINOR #3).

    `asyncio.gather` alone never proved the two `DELETE`s overlapped: nothing stopped the ASGI test
    transport from running one request to completion before the other's handler had even started,
    and in that case the loser's 404 would come from `GetTailoringRun` finding nothing (a
    *different* code path — the run simply is not there any more) rather than from
    `SqlAlchemyHistoryEntryData.delete_history_entry`'s `None` branch, which is what H-44 and this
    test's name are actually about.

    Staged so the ordering is provable rather than hoped for. `GetTailoringRun.__call__` (shared by
    both requests; patched once) is wrapped so that the **first** caller to return — having already
    read and authorized the run, while it still exists — pauses under `asyncio.wait_for` until the
    **second** caller has also read it, and only then proceeds. That guarantees the second request's
    read (and so its dispatch) lands strictly after the first's read and strictly before the first
    calls `delete_history_entry` — hence strictly before the first's commit — exactly the window the
    reviewer named. From that point the two `delete_history_entry` calls race for real, on two
    separate connections; which one wins is Postgres' own row lock on `tailoring_run`, not this test
    (the adapter's own docstring: "the second's DELETE matches nothing once the first commits").

    A `contextvars.ContextVar` tags each task's role ("first"/"second") across its own await chain —
    `asyncio.gather` gives each coroutine its own task and its own copy of the context, so the two
    never see each other's role — which lets the wrapped `delete_history_entry` and
    `LocalFileStore.delete` be asserted **per role**: the loser's `delete_history_entry` must have
    returned `None` (H-44's branch, never a `GetTailoringRun` 404) and the loser must never have
    called `LocalFileStore.delete` at all.

    Mutation check (run by hand, not committed — the file is restored byte-exact; `git status` is
    clean): flipping `delete_results[loser_role] is True` to `is False` makes this test fail on
    every run, which is the proof the assertion discriminates the branch and is not just checking
    the two status codes again under a longer docstring.
    """
    account, entry = await _seed_committed(concurrent_app, settings, engine, clock)

    role: contextvars.ContextVar[str] = contextvars.ContextVar("role", default="unset")
    order: list[str] = []
    first_read_done = asyncio.Event()
    second_read_done = asyncio.Event()
    reads_seen: list[str] = []
    delete_results: dict[str, bool] = {}
    unlink_calls: list[str] = []

    original_get = GetTailoringRun.__call__
    original_delete_entry = SqlAlchemyHistoryEntryData.delete_history_entry
    original_file_delete = LocalFileStore.delete

    async def _tracking_get(
        self: GetTailoringRun, run_id: TailoringRunId, requester: Owner
    ) -> TailoringRun:
        result = await original_get(self, run_id, requester)
        reads_seen.append(role.get())
        if len(reads_seen) == 1:
            order.append("first_read_run")
            first_read_done.set()
            await asyncio.wait_for(second_read_done.wait(), timeout=5)
        else:
            order.append("second_read_run")
            second_read_done.set()
        return result

    async def _tracking_delete_entry(
        self: SqlAlchemyHistoryEntryData, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        result = await original_delete_entry(self, user_id, run_id)
        delete_results[role.get()] = result is None
        return result

    async def _tracking_file_delete(self: LocalFileStore, ref: FileRef) -> None:
        unlink_calls.append(role.get())
        await original_file_delete(self, ref)

    async def _run_first() -> Response:
        role.set("first")
        async with new_client(concurrent_app) as client_one:
            return await client_one.delete(entry.run_url, headers=account.headers)

    async def _run_second() -> Response:
        await asyncio.wait_for(first_read_done.wait(), timeout=5)
        role.set("second")
        async with new_client(concurrent_app) as client_two:
            return await client_two.delete(entry.run_url, headers=account.headers)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GetTailoringRun, "__call__", _tracking_get)
            mp.setattr(SqlAlchemyHistoryEntryData, "delete_history_entry", _tracking_delete_entry)
            mp.setattr(LocalFileStore, "delete", _tracking_file_delete)
            response_first, response_second = await asyncio.wait_for(
                asyncio.gather(_run_first(), _run_second()), timeout=10
            )

        assert order == ["first_read_run", "second_read_run"], (
            "the second request's read must land between the first's read and its delete, but the "
            f"observed order was {order!r}"
        )
        results = {"first": response_first, "second": response_second}
        assert sorted(r.status_code for r in results.values()) == [204, 404], {
            k: v.text for k, v in results.items()
        }
        loser_role = next(k for k, v in results.items() if v.status_code == 404)
        winner_role = "second" if loser_role == "first" else "first"

        assert delete_results[loser_role] is True, (
            "the loser must reach delete_history_entry's None branch (H-44), not fail earlier"
        )
        assert delete_results[winner_role] is False
        assert error_code(results[loser_role]) == "tailoring_run_not_found"
        assert unlink_calls == [winner_role], "the loser must unlink nothing"
        assert not (settings.upload_dir / entry.jobs[0].storage_ref.key).exists()
    finally:
        await _drop_user(engine, account)


# --- Reviewer MINOR #1: a new run racing a posting deletion -----------------------------------
#
# `SqlAlchemyHistoryEntryData.delete_history_entry`'s posting `DELETE` used to be a bare
# `NOT EXISTS (SELECT … FROM tailoring_run WHERE job_posting_id = :p)` with no lock and no owner in
# the `NOT EXISTS`. `tailoring_run.job_posting_id` carries no FK (the mapping's own comment: "does
# not dangle in practice: a user-owned posting is deleted only with its last referencing run" — the
# claim this pair of tests is checking). A second run request (B, `POST /api/me/tailoring-runs`,
# authorizing the same posting P a first run's history entry (R1) already references) could race a
# `DELETE` of that entry (A): if B's `INSERT` of its own run (R2, on P) was still open —
# flushed inside a SAVEPOINT, uncommitted — when A's `NOT EXISTS` ran, A could not see R2, deleted P,
# and R2 ended up referencing a posting that no longer existed once B committed.
#
# The invariant both tests check is the same one either way: once both requests have committed, no
# `tailoring_run` row may reference a `posting_job_posting` row that does not exist, and neither
# request may answer 5xx.
#
# **The fix is landed, lock-based, infra-only, and unconditional** (3afc7da, then 3b57daf dropped
# the transitional `refuse_missing_posting` flag): `SqlAlchemyTailoringRunRepository.add` takes the
# run's posting `SELECT … FOR KEY SHARE` — **after its `INSERT`, never before** (the order is
# load-bearing: the `INSERT`'s own FK check takes the *owner* row first, so an account erasure or a
# guest purge always meets this method on the owner row before either touches the posting, and the
# two can queue but never deadlock — see `add`'s own docstring). No row → `JobPostingNotFound`, the
# same 404 `job_posting_not_found` the authorization would have given a moment later, and no run.
# `SqlAlchemyHistoryEntryData.delete_history_entry` takes `SELECT … FOR UPDATE` on the posting row,
# then issues the posting `DELETE … WHERE NOT EXISTS (run)` as a *separate* statement so it reads a
# fresh snapshot once it has the lock.
#
# Because the refusal is now unconditional, direction (i)'s A always **blocks** on B's
# `FOR KEY SHARE` until B commits or rolls back — a test that made B wait for A's whole request to
# *finish* before B is allowed to proceed would deadlock the two coroutines against each other. The
# test below still races "A finished" against "A is observed blocked on a lock" (so a regression that
# silently dropped the lock again fails on a clear assertion rather than hanging), but now asserts
# the blocked path is the one that actually fires, and — GREEN being in — asserts the one outcome
# each direction must produce rather than accepting either.


async def test_reviewer_minor1_insert_first_leaves_no_dangling_posting_reference(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """Direction (i), insert-first: B's `INSERT` (uncommitted, inside its SAVEPOINT) lands before
    A's `DELETE …/tailoring-runs/{R1}` reaches the posting.

    This needs **real concurrency**, not H-33's single-coroutine technique: B's transaction has to
    stay open while A's request is in flight, so two `asyncio` tasks trade control through `Event`s,
    each awaited under `asyncio.wait_for` — a hang here is a bug in the test, not a legitimate
    outcome, and must fail loudly rather than hang the suite.

    `SqlAlchemyTailoringRunRepository.add` is the pause point: by the time it is called, B's
    `RequestTailoringRun` use case has already authorized P through `GetJobPosting` (2.3's shared
    ownership check) and is about to flush the new run's `INSERT`. The wrapper lets the real `add`
    run to completion — the `INSERT` flushes, uncommitted, inside its own SAVEPOINT, and **then**
    (never before — `add`'s own docstring explains why the lock follows the `INSERT`, not the
    reverse) the row's posting is locked `FOR KEY SHARE` — then signals A.

    **B is released the moment A is observably blocked on that lock, or A finishes unblocked —
    the latter kept only as a regression trip-wire, never the expected path now that the refusal
    is unconditional.** `SqlAlchemyHistoryEntryData`'s `SELECT … FOR UPDATE` on the posting row
    conflicts with B's `FOR KEY SHARE`, so A always blocks until B commits or rolls back; this test
    polls `pg_stat_activity`, scoped to **A's own backend pid** (captured off A's own session the
    moment its `delete_history_entry` call starts — never any other backend's lock wait, so a
    concurrent test process or an unrelated idle-in-transaction connection cannot produce a false
    positive), for that one backend waiting on a lock, and releases B on that signal.
    `asyncio.wait(..., return_when=FIRST_COMPLETED)` races the two conditions — "A finished" vs. "A
    is blocked" — under one outer `asyncio.wait_for` so a design mistake in this test (neither ever
    becomes true) fails on a timeout rather than hanging the suite, and `order` records which one
    actually fired so a silent regression back to the unconditional-refusal-less shape shows up as
    an assertion naming the wrong path rather than a hang.

    Hardened now that GREEN has landed (3afc7da, 3b57daf): B must answer **202**, with R2 landed
    referencing P, P surviving, and `posting_deleted` **false** in A's own report — never "either
    path" the way this test accepted before the fix was unconditional.

    **Mutation proofs (run by hand, not committed — both source files restored byte-exact after;
    `git status -- api/src` empty):**

    - **m1** — `SqlAlchemyTailoringRunRepository.add`'s posting lock and `JobPostingNotFound`
      refusal removed (the flush kept, nothing after it). With no lock at all, A never observes a
      block — `order` ends `[..., "a_finished_unblocked"]` — and this test fails on the first
      hardened assertion:
      `AssertionError: the refusal is unconditional now (3afc7da, 3b57daf): A must always block on
      B's FOR KEY SHARE, never finish unblocked — observed order was ['b_inserted_uncommitted',
      'a_delete_issued', 'a_finished_unblocked']`. (Its sibling below, delete-first, is the
      assertion this mutation is really aimed at, and goes red there instead — see that test's own
      docstring.)
    - **m2** — the delete side's `SELECT … FOR UPDATE` pre-lock removed, leaving the single
      `DELETE … WHERE NOT EXISTS (run)` statement. A no longer waits for B, so its `NOT EXISTS`
      reads the pre-wait snapshot and deletes P before B ever commits R2. Red on the posting's
      survival:
      `AssertionError: the posting must survive: R2 committed to reference it while A's deletion
      was blocked waiting on B's lock
      assert 0 == 1`.
    """
    account, entry = await _seed_committed(concurrent_app, settings, engine, clock)
    queue = FakeTailoringQueue()
    concurrent_app.dependency_overrides[get_tailoring_queue] = lambda: queue

    order: list[str] = []
    b_inserted_uncommitted = asyncio.Event()
    a_finished = asyncio.Event()
    b_may_proceed = asyncio.Event()
    a_backend_pid: list[int] = []
    a_report: list[DeletedHistoryEntry | None] = []
    original_add = SqlAlchemyTailoringRunRepository.add
    original_delete_entry = SqlAlchemyHistoryEntryData.delete_history_entry

    async def _pause_after_uncommitted_insert(
        self: SqlAlchemyTailoringRunRepository, run: TailoringRun
    ) -> None:
        await original_add(self, run)
        order.append("b_inserted_uncommitted")
        b_inserted_uncommitted.set()
        await asyncio.wait_for(b_may_proceed.wait(), timeout=5)

    async def _capture_pid_then_delete(
        self: SqlAlchemyHistoryEntryData, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        """Captures A's own backend pid off A's own session — the same connection about to run the
        `FOR UPDATE` — before delegating to the real adapter, so `_a_is_blocked_on_a_lock` can scope
        its wait to exactly that one backend. Also captures the adapter's own report, so the test
        can assert `posting_deleted` on the thing A actually decided, not re-derive it."""
        connection = await self._session.connection()
        pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        a_backend_pid.append(pid)
        result = await original_delete_entry(self, user_id, run_id)
        a_report.append(result)
        return result

    async def _run_b() -> Response:
        async with new_client(concurrent_app) as client_b:
            return await client_b.post(
                ME_RUNS,
                json={
                    "base_cv_id": str(entry.cv_id.value),
                    "job_posting_id": str(entry.posting_id.value),
                },
                headers=account.headers,
            )

    async def _run_a() -> Response:
        await asyncio.wait_for(b_inserted_uncommitted.wait(), timeout=5)
        order.append("a_delete_issued")
        async with new_client(concurrent_app) as client_a:
            response = await client_a.delete(entry.run_url, headers=account.headers)
        a_finished.set()
        return response

    async def _a_is_blocked_on_a_lock() -> bool:
        """Polls a separate connection — never A's or B's own — for **A's own backend pid**
        (`a_backend_pid`, set by `_capture_pid_then_delete`) waiting on a lock: the adapter's
        `SELECT … FOR UPDATE`, parked behind B's `FOR KEY SHARE`. Scoped to that one pid rather than
        "any backend of this database waiting on a lock" (reviewer /verify round 2), so an unrelated
        connection — another test's, a stray idle-in-transaction session — cannot produce a false
        positive; it also means this can only return `True` once A's request has actually reached
        `delete_history_entry`, so it polls with the pid unset until then."""
        while True:
            if a_backend_pid:
                async with engine.connect() as probe:
                    blocked = await probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE pid = :pid AND wait_event_type = 'Lock' AND state = 'active'"
                        ),
                        {"pid": a_backend_pid[0]},
                    )
                    if blocked.scalar_one() > 0:
                        return True
            await asyncio.sleep(0.02)

    async def _wait_for_a_blocked_or_finished() -> None:
        blocked_task = asyncio.ensure_future(_a_is_blocked_on_a_lock())
        finished_task = asyncio.ensure_future(a_finished.wait())
        try:
            done, _pending = await asyncio.wait(
                {blocked_task, finished_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (blocked_task, finished_task):
                if not task.done():
                    task.cancel()
        order.append(
            "a_observed_blocked_on_lock" if blocked_task in done else "a_finished_unblocked"
        )

    task_b: asyncio.Task[Response] | None = None
    task_a: asyncio.Task[Response] | None = None
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(SqlAlchemyTailoringRunRepository, "add", _pause_after_uncommitted_insert)
            mp.setattr(SqlAlchemyHistoryEntryData, "delete_history_entry", _capture_pid_then_delete)
            task_b = asyncio.ensure_future(_run_b())
            task_a = asyncio.ensure_future(_run_a())
            try:
                await asyncio.wait_for(b_inserted_uncommitted.wait(), timeout=5)
                await asyncio.wait_for(_wait_for_a_blocked_or_finished(), timeout=5)
                b_may_proceed.set()
                response_b, response_a = await asyncio.wait_for(
                    asyncio.gather(task_b, task_a), timeout=10
                )
            finally:
                # A timed-out wait_for above must not leave a request running in the background
                # once this test moves on (and `finally` below deletes the user out from under it):
                # cancel whatever is still in flight and wait it out before continuing.
                for task in (task_b, task_a):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(task_b, task_a, return_exceptions=True)

        assert order[0] == "b_inserted_uncommitted", order
        assert "a_delete_issued" in order, (
            "A's DELETE must have been issued while B's insert was still open and uncommitted, but "
            f"the observed order was {order!r}"
        )
        assert order.index("a_delete_issued") > order.index("b_inserted_uncommitted"), order
        assert "a_observed_blocked_on_lock" in order, (
            "the refusal is unconditional now (3afc7da, 3b57daf): A must always block on B's "
            f"FOR KEY SHARE, never finish unblocked — observed order was {order!r}"
        )

        assert response_a.status_code == 204, response_a.text
        assert response_b.status_code == 202, response_b.text
        run2_id = UUID(response_b.json()["id"])
        assert queue.enqueued == [TailoringRunId(run2_id)], (
            "the accepted run must be the one actually enqueued"
        )

        async with engine.connect() as reader:
            posting_still_exists = await reader.execute(
                text("SELECT count(*) FROM posting_job_posting WHERE id = :id"),
                {"id": entry.posting_id.value},
            )
            dangling = await reader.execute(
                text(
                    "SELECT count(*) FROM tailoring_run t "
                    "LEFT JOIN posting_job_posting p ON p.id = t.job_posting_id "
                    "WHERE t.id = :run2_id AND p.id IS NULL"
                ),
                {"run2_id": run2_id},
            )
        assert posting_still_exists.scalar_one() == 1, (
            "the posting must survive: R2 committed to reference it while A's deletion was "
            "blocked waiting on B's lock"
        )
        assert dangling.scalar_one() == 0, (
            "no tailoring_run row may reference a nonexistent posting_job_posting row"
        )
        assert a_report, "A's delete_history_entry was never called"
        assert a_report[0] is not None, "A's delete must have succeeded"
        assert a_report[0].posting_deleted is False, (
            "A must have kept the posting once its re-read, after the lock was granted, saw R2"
        )
    finally:
        await _drop_user(engine, account)


async def test_reviewer_minor1_delete_first_leaves_no_dangling_posting_reference(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """Direction (ii), delete-first: A's whole `DELETE` (a real, separate connection, a real commit)
    lands strictly between B's posting authorization read and B's `INSERT` — the opposite ordering
    from the test above.

    This one does not need real concurrency: A's request can run to completion **inside** B's own
    coroutine, synchronously, the same technique 1.4/H-33 use for a bare `DELETE`, just with a whole
    nested request in place of one statement. `GetJobPosting.__call__` is the seam — B's
    `RequestTailoringRun` calls it to authorize P and does nothing else with the database until its
    own `INSERT` — so the wrapper lets the real authorization read return, then issues A's `DELETE`
    against `concurrent_app` and awaits it fully (so A's commit is real and already landed) before
    handing the posting back to B, which then proceeds to try to insert its run.

    Hardened now that GREEN has landed (3afc7da, 3b57daf): B's `SELECT … FOR KEY SHARE` runs after
    P is already committed-gone, finds no row, and the SAVEPOINT's rollback discards the flushed
    run — so B must answer **404 `job_posting_not_found`**, with no R2 anywhere, never "either
    path" the way this test accepted before the fix was unconditional.

    **Mutation proof m1 (run by hand, not committed — `api/src` restored byte-exact after;
    `git status -- api/src` empty):** `SqlAlchemyTailoringRunRepository.add`'s posting lock and
    `JobPostingNotFound` refusal removed. With nothing to refuse the insert, B succeeds over P's
    already-vanished id — the exact dangling reference this pair of tests exists to catch — and
    this is the test that goes red on it, on the hardened status assertion:
    `AssertionError: assert 202 == 404` (full body: a `202` carrying a freshly `queued` run whose
    `job_posting_id` is the now-deleted P). m2 (the delete side's `FOR UPDATE` pre-lock removed)
    does not move this test — A already ran to completion, alone, with no contention to wait out —
    and is recorded on its sibling, insert-first, instead.
    """
    account, entry = await _seed_committed(concurrent_app, settings, engine, clock)
    queue = FakeTailoringQueue()
    concurrent_app.dependency_overrides[get_tailoring_queue] = lambda: queue

    original_get_job_posting = GetJobPosting.__call__
    delete_responses: list[Response] = []

    async def _delete_between_authorize_and_insert(
        self: GetJobPosting, job_posting_id: JobPostingId, requester: Owner
    ) -> JobPosting:
        posting = await original_get_job_posting(self, job_posting_id, requester)
        async with new_client(concurrent_app) as client_a:
            delete_responses.append(await client_a.delete(entry.run_url, headers=account.headers))
        return posting

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GetJobPosting, "__call__", _delete_between_authorize_and_insert)
            async with new_client(concurrent_app) as client_b:
                response_b = await client_b.post(
                    ME_RUNS,
                    json={
                        "base_cv_id": str(entry.cv_id.value),
                        "job_posting_id": str(entry.posting_id.value),
                    },
                    headers=account.headers,
                )

        assert len(delete_responses) == 1, "A's delete must run exactly once, mid-request"
        assert delete_responses[0].status_code == 204, delete_responses[0].text
        async with engine.connect() as reader:
            posting_gone = await reader.execute(
                text("SELECT count(*) FROM posting_job_posting WHERE id = :id"),
                {"id": entry.posting_id.value},
            )
        assert posting_gone.scalar_one() == 0, (
            "A's delete-and-commit must already have landed before B's insert is attempted"
        )

        assert response_b.status_code == 404, response_b.text
        assert error_code(response_b) == "job_posting_not_found"
        assert queue.enqueued == [], "a refused request must never reach the enqueue"
        async with engine.connect() as reader:
            no_run = await reader.execute(
                text(
                    "SELECT count(*) FROM tailoring_run WHERE user_id = :u AND job_posting_id = :p"
                ),
                {"u": account.user_id.value, "p": entry.posting_id.value},
            )
        assert no_run.scalar_one() == 0, (
            "R2 must not exist: the SAVEPOINT's rollback discards the flushed run along with the "
            "refusal"
        )
    finally:
        await _drop_user(engine, account)
