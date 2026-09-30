"""`SqlAlchemyAccountData` — the `AccountDataPort` adapter (slice 2.2, technical plan §0.4, §3).

Account erasure is the purge's sibling, and this module is `expired_guest_data.py`'s sibling: Core
SQL only, every column named, **no `SELECT *` anywhere**, and nothing hydrated. Erasure never needs
a `User`, a `Login` or a `BaseCv` — only whether the user exists, the storage keys its rows name, and
a count or two — so loading an aggregate would buy nothing but a result set holding a password hash
or a CV's text, one `repr()` away from a log line. The columns read below are ids, one storage key,
an export format and counts.

**Since slice 2.3 an account is also its history** (ADR-0023, AC-15): its tailoring runs, job
postings and export jobs cascade from `identity_user` with its saved CVs, and its export files are
collected beside the CVs' — each key derived from its job's `(id, format)`, the purge's rule.

**Nothing here logs**, as in the purge's adapter: the one line per erasure is the entry point's, built
from the `AccountErasureReport` the use case returns.

**The unit of work is the caller's.** Nothing here commits; `CommittingAccountData`
(`infrastructure/retention/data_access.py`) commits after `delete_account`, which is what makes "rows
first, committed, then files" true of an erasure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from sqlalchemy import ColumnElement, ScalarSelect, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat, ExportJobId
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountCounts
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.login import login_table
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import AccountDataPort

# The formats an export job can have a file for, asked of `ExportFormat` rather than written out —
# `expired_guest_data.py`'s `_QUEUED_FORMATS`, with its reasoning. A row for an inline format is
# impossible (`ExportJob.request`, `ck_export_job_format_is_queued`) and would have no file, so it
# contributes no key; filtering it in SQL keeps `FileRef.for_export` from raising mid-erasure over a
# row with nothing to unlink. `count_account` filters `files` by the same tuple, so the dry run's
# count and the real run's keys cannot disagree.
_QUEUED_FORMATS: Final[tuple[ExportFormat, ...]] = tuple(
    fmt for fmt in ExportFormat if fmt.delivery is ExportDelivery.QUEUED
)


class SqlAlchemyAccountData:
    """Everything erasure asks the store of record about one account, over one `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        """Lock the user row, then collect every storage key the account's rows name.

        1. `SELECT id FROM identity_user WHERE id = :u FOR UPDATE` — no row → `AccountNotFound`.
        2. `SELECT file_key FROM intake_base_cv WHERE user_id = :u`, over `ix_intake_base_cv_user_id`.
        3. `SELECT id, format FROM export_job WHERE user_id = :u AND format IN (<queued>)`, over
           `ix_export_job_user_id` (slice 2.3, AC-15) → `FileRef.for_export(id, format)` each.
           **Derived, never `file_key`**: a `rendering` job has written bytes and no key yet, and a
           `failed` one may have left a partial — the purge's rule (ADR-0018 decision 3). Every job
           of the account, including one whose run is gone (H-49's residual row).

        **The lock is the port's completeness promise, kept** (AC-32). Rows-first leaves one race
        open: an account upload that inserts a saved CV *after* step 2 and *before* the delete would
        be cascaded away with its file never collected — bytes on disk that no row and no report
        names. Its `INSERT` must take `FOR KEY SHARE` on this user row for the FK check, which
        conflicts with `FOR UPDATE`, so it waits until the erasure commits, then fails on
        `fk_intake_base_cv_user_id_identity_user` (→ `UserNotFound` → 401, S-12). Its already-written
        file is an orphan for the sweep. The lock is held until the transaction ends, which is
        `CommittingAccountData.delete_account`'s commit.

        **Since 2.3 the same lock serializes run, posting and export `INSERT`s** (AC-37): each of
        their `user_id` FKs takes `FOR KEY SHARE` on this row too, so a render requested mid-erasure
        cannot add a job — and a file — after step 3 read the keys; it waits, then fails on
        `fk_export_job_user_id_identity_user` (→ `UserNotFound`, H-53).

        **It also serialises two erasures of one account** (S-46). The second waits here, and once
        the first commits, READ COMMITTED re-checks the locked row, finds it deleted, and returns
        nothing: `AccountNotFound`, before the loser has collected a key it might unlink twice.

        `FOR UPDATE` rather than `FOR NO KEY UPDATE`: the latter does not conflict with `FOR KEY
        SHARE`, so it would let the racing `INSERT` through — the exact race this closes. And a lock
        on one user row, never on `intake_base_cv`: a table lock would stall every guest upload on
        the box for the length of someone else's erasure.
        """
        locked = await self._session.execute(
            select(user_table.c.id).where(user_table.c.id == user_id).with_for_update()
        )
        if locked.scalar_one_or_none() is None:
            raise AccountNotFound()

        cv_keys = await self._session.execute(
            select(base_cv_table.c.file_key).where(base_cv_table.c.user_id == user_id)
        )
        jobs = await self._session.execute(
            select(export_job_table.c.id, export_job_table.c.format).where(
                export_job_table.c.user_id == user_id,
                export_job_table.c.format.in_(_QUEUED_FORMATS),
            )
        )
        export_keys: list[FileRef] = []
        for row in jobs:
            job_id: ExportJobId = row.id
            job_format: ExportFormat = row.format
            export_keys.append(FileRef.for_export(job_id, job_format))
        return (*cv_keys.scalars().all(), *export_keys)

    async def delete_account(self, user_id: UserId) -> bool:
        """`DELETE FROM identity_user WHERE id = :u`. `True` if a row went.

        **One statement**; the rest goes by `ON DELETE CASCADE`: `identity_login` (and through it
        `identity_retired_refresh_token`), `intake_base_cv`, and since slice 2.3 the account's
        history — `tailoring_run`, `posting_job_posting` and `export_job`, each through its own
        `fk_<table>_user_id_identity_user`. Three independent cascades from the user row, not a
        chain: there is no FK between those three tables (ADR-0014, ADR-0016). Rows only — never a
        file; `files_of_account` collected the export keys first.

        The row count is read from the statement itself, on the session's connection, rather than
        from a check beforehand: `False` means a concurrent erasure deleted it first, and a check
        followed by a delete would be the race.

        **The `User` leaves the identity map**, if one is there — `DeleteOwnAccount` loaded it to
        verify the password. A Core `DELETE` does not tell the ORM the row went, and a later flush of
        that instance (clean today; not a promise anyone made) would target a row that no longer
        exists. Expunged by identity, as `SqlAlchemyLoginRepository.remove` does.
        """
        stale = self._session.identity_map.get(identity_key(User, user_id))
        if stale is not None:
            self._session.expunge(stale)

        connection = await self._session.connection()
        result = await connection.execute(delete(user_table).where(user_table.c.id == user_id))
        return result.rowcount > 0

    async def count_account(self, user_id: UserId) -> AccountCounts | None:
        """What `delete_account` would take, in one statement, for `erase-account --dry-run`.

        `SELECT (count …), … FROM identity_user WHERE id = :u` — scalar subqueries over the user
        row, so "no such user" is simply no row (`None`, not an exception: reporting on an absent
        account is an ordinary outcome of looking). Deletes nothing and takes no lock: a dry run is a
        report, and a report that blocks an upload is worse than one that is a moment stale. Each
        count seeks its table's `user_id` index.

        `files` is the saved CVs' `count(file_key)` **plus** the export jobs that have a derived key
        (slice 2.3, AC-36) — the same two sets `files_of_account` returns, filtered by the same
        `_QUEUED_FORMATS`, so a dry run and the real run agree on the number. That is the day 2.2's
        docstring predicted `files` and `base_cvs` would stop being the same number.
        """

        def count_of(table_user_id: ColumnElement[UserId | None]) -> ScalarSelect[int]:
            return select(func.count()).where(table_user_id == user_id).scalar_subquery()

        cv_files = (
            select(func.count(base_cv_table.c.file_key))
            .where(base_cv_table.c.user_id == user_id)
            .scalar_subquery()
        )
        export_files = (
            select(func.count())
            .where(
                export_job_table.c.user_id == user_id,
                export_job_table.c.format.in_(_QUEUED_FORMATS),
            )
            .scalar_subquery()
        )
        row = (
            await self._session.execute(
                select(
                    count_of(base_cv_table.c.user_id).label("base_cvs"),
                    (cv_files + export_files).label("files"),
                    count_of(login_table.c.user_id).label("logins"),
                    count_of(tailoring_run_table.c.user_id).label("tailoring_runs"),
                    count_of(job_posting_table.c.user_id).label("job_postings"),
                    count_of(export_job_table.c.user_id).label("export_jobs"),
                ).where(user_table.c.id == user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return AccountCounts(
            base_cvs=row.base_cvs,
            files=row.files,
            logins=row.logins,
            tailoring_runs=row.tailoring_runs,
            job_postings=row.job_postings,
            export_jobs=row.export_jobs,
        )


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_account_data(adapter: SqlAlchemyAccountData) -> None:
        _: AccountDataPort = adapter
