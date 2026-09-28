"""Persistence tests for `SqlAlchemyAccountData` — the `AccountDataPort` adapter (T15, after, slice
2.2): `files_of_account`'s keys, `delete_account`'s cascade, `count_account`'s dry-run counts, and
**AC-32's row-lock race against two genuinely independent connections.**

Most of this file uses the ordinary rolled-back `session`/`connection` fixtures — a single session's
own transaction is all `files_of_account`, `delete_account` and `count_account` need to prove their
own SQL correct. **AC-32 is the one test that cannot**: it needs a real `FOR UPDATE` held open on one
physical connection while a second, independent connection's `INSERT` genuinely blocks on it and then
genuinely fails once the first commits — exactly the reasoning `test_purge_database.py`'s AC-13 test
gives for pinning its own connections rather than trusting the shared, SAVEPOINT-bound `session`
fixture. That test's `committing_connection`/`committing_session` shape is not reused directly (it is
`test_purge_database.py`-local on purpose — its own module docstring explains why promoting it would
force two different data-building needs onto one helper); this file reproduces the same small pattern
for its own two-connection scenario instead.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import Table, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, TokenHash, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountCounts
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.login import login_table
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.login import (
    SqlAlchemyLoginRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
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
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import pasted_posting, queued_export, ready_export, succeeded_run
from tests.integration.persistence.owner_rows import persist_guest

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")
_LOGIN_LIFETIME = timedelta(days=30)
_STATEMENT_TIMEOUT_MS: Final = 5_000
"""A fail-fast backstop on every connection AC-32's test pins for itself, matching
`test_purge_database.py`'s identical constant and its reason: this test's whole point is a genuine
block, and a regression that turns "blocks, then fails" into "hangs forever" must fail in seconds,
not sit until CI's own timeout with a failure that names nothing."""


def _assert_test_database(settings: Settings) -> None:
    """The 1.4 guard (CLAUDE.md), reproduced locally like every other file in this package that
    commits for real off a pinned connection rather than the rolled-back `session` fixture."""
    assert "_test" in settings.database_url, (
        "refusing to run a committing account-data test against a URL that is not the test "
        f"database: {settings.database_url!r}"
    )


async def _persist_user(session: AsyncSession, clock: FixedClock, *, email: str) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(email),
            password_hash=_PASSWORD_HASH,
            at=clock.now(),
        )
    )
    return user_id


def _saved_cv(cvs: SqlAlchemyBaseCvRepository, owner: UserId, clock: FixedClock) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        owner=UserOwner(owner),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )


# --- files_of_account -------------------------------------------------------------------------


async def test_files_of_account_returns_every_saved_cvs_key(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="files-of-account@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    first = _saved_cv(cvs, user_id, clock)
    second = _saved_cv(cvs, user_id, clock)
    await cvs.add(first)
    await cvs.add(second)
    other_user = await _persist_user(session, clock, email="decoy@example.com")
    await cvs.add(_saved_cv(cvs, other_user, clock))

    accounts = SqlAlchemyAccountData(session)
    keys = await accounts.files_of_account(user_id)

    assert set(keys) == {first.file, second.file}


async def test_files_of_account_returns_empty_for_a_user_with_no_saved_cvs(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="no-cvs@example.com")
    accounts = SqlAlchemyAccountData(session)

    assert await accounts.files_of_account(user_id) == ()


async def test_files_of_account_raises_account_not_found_for_a_nonexistent_user(
    session: AsyncSession,
) -> None:
    accounts = SqlAlchemyAccountData(session)
    with pytest.raises(AccountNotFound):
        await accounts.files_of_account(UserId(value=uuid4()))


# --- delete_account ------------------------------------------------------------------------------


async def test_delete_account_removes_the_user_and_cascades_logins_and_saved_cvs(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="delete-account@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_cv(cvs, user_id, clock)
    await cvs.add(cv)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("a" * 64),
        at=clock.now(),
        lifetime=_LOGIN_LIFETIME,
    )
    await logins.add(login)
    await session.flush()

    accounts = SqlAlchemyAccountData(session)
    result = await accounts.delete_account(user_id)

    assert result is True
    remaining_user = await session.execute(
        select(func.count()).select_from(user_table).where(user_table.c.id == user_id)
    )
    assert remaining_user.scalar_one() == 0
    remaining_cv = await session.execute(
        select(func.count()).select_from(base_cv_table).where(base_cv_table.c.id == cv.id)
    )
    assert remaining_cv.scalar_one() == 0
    remaining_login = await session.execute(
        select(func.count()).select_from(login_table).where(login_table.c.id == login.id)
    )
    assert remaining_login.scalar_one() == 0


async def test_delete_account_returns_false_for_a_nonexistent_user(session: AsyncSession) -> None:
    accounts = SqlAlchemyAccountData(session)
    assert await accounts.delete_account(UserId(value=uuid4())) is False


# --- count_account --------------------------------------------------------------------------------


async def test_count_account_counts_base_cvs_files_and_logins(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="count-account@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    await cvs.add(_saved_cv(cvs, user_id, clock))
    await cvs.add(_saved_cv(cvs, user_id, clock))
    logins = SqlAlchemyLoginRepository(session)
    await logins.add(
        Login.start(
            id=logins.next_identity(),
            user_id=user_id,
            token_hash=TokenHash("b" * 64),
            at=clock.now(),
            lifetime=_LOGIN_LIFETIME,
        )
    )
    await session.flush()

    accounts = SqlAlchemyAccountData(session)
    counts = await accounts.count_account(user_id)

    assert counts == AccountCounts(
        base_cvs=2, files=2, logins=1, tailoring_runs=0, job_postings=0, export_jobs=0
    )


async def test_count_account_returns_none_for_a_nonexistent_user(session: AsyncSession) -> None:
    accounts = SqlAlchemyAccountData(session)
    assert await accounts.count_account(UserId(value=uuid4())) is None


# --- AC-32: a concurrent upload racing an erasure cannot leave a row ------------------------------


async def _committed_user(engine: AsyncEngine, clock: FixedClock, *, email: str) -> UserId:
    """A real, committed `User` off its own short-lived connection — AC-32 needs a row visible to
    two *independent* connections, which the rolled-back `session` fixture cannot provide."""
    async with engine.connect() as conn:
        session = async_sessionmaker(bind=conn, expire_on_commit=False, autoflush=False)()
        try:
            user_id = await _persist_user(session, clock, email=email)
            await session.commit()
        finally:
            await session.close()
    return user_id


async def test_ac32_an_upload_racing_an_erasure_finds_no_owner_and_leaves_no_row(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """AC-32. `SqlAlchemyAccountData.files_of_account` locks the `identity_user` row `FOR UPDATE`
    before collecting keys (technical plan §0.4). A concurrent account upload's `INSERT` needs `FOR
    KEY SHARE` on that same row for its foreign-key check, so it waits; once the erasure commits, the
    row is gone and the FK refuses, which `SqlAlchemyBaseCvRepository.add` translates to
    `UserNotFound` (S-12) — never a row and never a stray file left with nothing pointing at it in
    the database's own bookkeeping (the file itself is a filesystem question outside this test's
    scope; S-12's point is the row).

    Proven with **two real connections**, never a mock: `lock_conn` holds the `FOR UPDATE` open while
    `upload_conn`'s `add()` genuinely blocks on it in a concurrent `asyncio.Task`, is genuinely
    unblocked by `lock_conn`'s commit, and genuinely fails.
    """
    _assert_test_database(settings)
    user_id = await _committed_user(engine, clock, email="ac32-race@example.com")

    async with engine.connect() as lock_conn:
        await lock_conn.execute(text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'"))
        await lock_conn.commit()
        lock_session = async_sessionmaker(bind=lock_conn, expire_on_commit=False, autoflush=False)()
        try:
            accounts = SqlAlchemyAccountData(lock_session)
            # Holds the FOR UPDATE lock open — no commit yet.
            await accounts.files_of_account(user_id)

            async with engine.connect() as upload_conn:
                await upload_conn.execute(
                    text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
                )
                await upload_conn.commit()
                upload_session = async_sessionmaker(
                    bind=upload_conn, expire_on_commit=False, autoflush=False
                )()
                try:
                    cvs = SqlAlchemyBaseCvRepository(upload_session)
                    racing_cv = _saved_cv(cvs, user_id, clock)

                    upload_task = asyncio.create_task(cvs.add(racing_cv))
                    # Give the racing INSERT time to actually reach Postgres and block on the row
                    # lock before the erasure below removes what it is waiting for — without this,
                    # `delete_account` could win a race that was never actually contended.
                    await asyncio.sleep(0.2)
                    assert not upload_task.done(), (
                        "test setup: the racing upload finished before the erasure committed — "
                        "it never actually contended the lock, so this test proves nothing"
                    )

                    deleted = await accounts.delete_account(user_id)
                    assert deleted is True
                    await lock_session.commit()  # releases the FOR UPDATE lock

                    with pytest.raises(UserNotFound):
                        await upload_task
                finally:
                    await upload_session.close()
        finally:
            await lock_session.close()

    async with engine.connect() as verify_conn:
        remaining_users = await verify_conn.execute(
            select(func.count()).select_from(user_table).where(user_table.c.id == user_id)
        )
        assert remaining_users.scalar_one() == 0
        remaining_cvs = await verify_conn.execute(
            select(func.count())
            .select_from(base_cv_table)
            .where(base_cv_table.c.user_id == user_id)
        )
        assert remaining_cvs.scalar_one() == 0, (
            "the racing upload left a row behind despite its INSERT failing"
        )


# --- Slice 2.3 (T17): the account's history ----------------------------------------------------------


async def _history(
    session: AsyncSession, clock: FixedClock, owner: UserOwner | GuestOwner
) -> tuple[TailoringRun, list[ExportJob]]:
    """A posting, a run over it, and a `ready`, a `rendering` and a `failed` export job."""
    posting = pasted_posting(owner, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(owner, clock.now(), job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    ready = ready_export(owner, run, clock.now(), format=ExportFormat.PDF)
    rendering = queued_export(owner, run, clock.now(), format=ExportFormat.DOCX)
    rendering.mark_started(clock.now())
    failed = queued_export(owner, run, clock.now(), format=ExportFormat.PDF)
    failed.mark_started(clock.now())
    failed.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    jobs = SqlAlchemyExportJobRepository(session)
    for job in (ready, rendering, failed):
        await jobs.add(job)
    await session.flush()
    return run, [ready, rendering, failed]


async def test_files_of_account_adds_every_export_jobs_derived_key_even_without_its_run(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-15: the export keys are **derived** from `(id, format)`, never read from `file_key`, so a
    `rendering` or `failed` job's bytes are collected too — and a job whose run row is already gone
    (H-49's residual) still names its file. Another user's and a guest's jobs are not included."""
    user_id = await _persist_user(session, clock, email="history-files@example.com")
    user = UserOwner(user_id)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_cv(cvs, user_id, clock)
    await cvs.add(cv)
    run, jobs = await _history(session, clock, user)
    await _history(
        session, clock, UserOwner(await _persist_user(session, clock, email="x@example.com"))
    )
    await _history(session, clock, await persist_guest(session, clock))
    await session.execute(tailoring_run_table.delete().where(tailoring_run_table.c.id == run.id))

    keys = await SqlAlchemyAccountData(session).files_of_account(user_id)

    assert sorted(ref.key for ref in keys) == sorted(
        [cv.file.key, *(FileRef.for_export(job.id, job.format).key for job in jobs)]
    )


async def test_count_account_counts_the_history_and_files_matches_files_of_account(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="history-count@example.com")
    user = UserOwner(user_id)
    cvs = SqlAlchemyBaseCvRepository(session)
    await cvs.add(_saved_cv(cvs, user_id, clock))
    await _history(session, clock, user)
    await _history(session, clock, user)
    await _history(session, clock, await persist_guest(session, clock))

    accounts = SqlAlchemyAccountData(session)
    counts = await accounts.count_account(user_id)
    files = await accounts.files_of_account(user_id)

    assert counts == AccountCounts(
        base_cvs=1, files=7, logins=0, tailoring_runs=2, job_postings=2, export_jobs=6
    )
    assert counts.files == len(files)


async def test_delete_account_cascades_the_users_history_and_spares_a_guests(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="history-cascade@example.com")
    mine, my_jobs = await _history(session, clock, UserOwner(user_id))
    theirs, their_jobs = await _history(session, clock, await persist_guest(session, clock))

    assert await SqlAlchemyAccountData(session).delete_account(user_id) is True

    async def exists(table: Table, row_id: object) -> bool:
        found = await session.execute(
            select(func.count()).select_from(table).where(table.c.id == row_id)
        )
        return found.scalar_one() == 1

    assert not await exists(tailoring_run_table, mine.id)
    assert not await exists(job_posting_table, mine.job_posting_id)
    assert not any([await exists(export_job_table, job.id) for job in my_jobs])
    assert await exists(tailoring_run_table, theirs.id)
    assert await exists(job_posting_table, theirs.job_posting_id)
    assert all([await exists(export_job_table, job.id) for job in their_jobs])
