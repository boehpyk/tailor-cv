"""`SqlAlchemyAccountData` — the `AccountDataPort` adapter (slice 2.2, technical plan §0.4, §3).

Account erasure is the purge's sibling, and this module is `expired_guest_data.py`'s sibling: Core
SQL only, every column named, **no `SELECT *` anywhere**, and nothing hydrated. Erasure never needs
a `User`, a `Login` or a `BaseCv` — only whether the user exists, the storage keys its rows name, and
a count or two — so loading an aggregate would buy nothing but a result set holding a password hash
or a CV's text, one `repr()` away from a log line. The columns read below are two ids, one storage
key and three counts.

**Nothing here logs**, as in the purge's adapter: the one line per erasure is the entry point's, built
from the `AccountErasureReport` the use case returns.

**The unit of work is the caller's.** Nothing here commits; `CommittingAccountData`
(`infrastructure/retention/data_access.py`) commits after `delete_account`, which is what makes "rows
first, committed, then files" true of an erasure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountCounts
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.persistence.mapping.identity.login import login_table
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import AccountDataPort


class SqlAlchemyAccountData:
    """Everything erasure asks the store of record about one account, over one `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        """Lock the user row, then collect every storage key the account's rows name.

        1. `SELECT id FROM identity_user WHERE id = :u FOR UPDATE` — no row → `AccountNotFound`.
        2. `SELECT file_key FROM intake_base_cv WHERE user_id = :u`, over `ix_intake_base_cv_user_id`.

        **The lock is the port's completeness promise, kept** (AC-32). Rows-first leaves one race
        open: an account upload that inserts a saved CV *after* step 2 and *before* the delete would
        be cascaded away with its file never collected — bytes on disk that no row and no report
        names. Its `INSERT` must take `FOR KEY SHARE` on this user row for the FK check, which
        conflicts with `FOR UPDATE`, so it waits until the erasure commits, then fails on
        `fk_intake_base_cv_user_id_identity_user` (→ `UserNotFound` → 401, S-12). Its already-written
        file is an orphan for the sweep. The lock is held until the transaction ends, which is
        `CommittingAccountData.delete_account`'s commit.

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

        keys = await self._session.execute(
            select(base_cv_table.c.file_key).where(base_cv_table.c.user_id == user_id)
        )
        return tuple(keys.scalars().all())

    async def delete_account(self, user_id: UserId) -> bool:
        """`DELETE FROM identity_user WHERE id = :u`. `True` if a row went.

        **One statement**; the rest goes by `ON DELETE CASCADE`: `identity_login` (and through it
        `identity_retired_refresh_token`) and `intake_base_cv`. Rows only — never a file.

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

        `SELECT (count …), (count …), (count …) FROM identity_user WHERE id = :u` — three scalar
        subqueries over the user row, so "no such user" is simply no row (`None`, not an exception:
        reporting on an absent account is an ordinary outcome of looking). Deletes nothing and takes
        no lock: a dry run is a report, and a report that blocks an upload is worse than one that is
        a moment stale.

        `files` is `count(file_key)` beside `base_cvs`' `count(*)`. They are equal today — every
        saved CV names exactly one file (`file_key` is `NOT NULL` and unique) — and they are separate
        numbers because they answer separate questions, which stop being the same the day another
        user-owned table holds a file (2.3).
        """
        saved = select(func.count()).where(base_cv_table.c.user_id == user_id).scalar_subquery()
        files = (
            select(func.count(base_cv_table.c.file_key))
            .where(base_cv_table.c.user_id == user_id)
            .scalar_subquery()
        )
        logins = select(func.count()).where(login_table.c.user_id == user_id).scalar_subquery()
        row = (
            await self._session.execute(
                select(saved.label("base_cvs"), files.label("files"), logins.label("logins")).where(
                    user_table.c.id == user_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return AccountCounts(base_cvs=row.base_cvs, files=row.files, logins=row.logins)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_account_data(adapter: SqlAlchemyAccountData) -> None:
        _: AccountDataPort = adapter
