"""`SqlAlchemyExpiredIdentityTokens` — the `ExpiredIdentityTokenPort` adapter (slice 2.5, technical
plan §0.9).

`expired_guest_data.py`'s sibling for identity's one-time rows: Core SQL only, nothing hydrated, no
`SELECT *`. The sweep never needs a `PendingRegistration`, a `PasswordReset` or a `Login` — only
"delete the next `n` that have expired" — and loading one would put an address and a password hash in
a result set for nothing.

**Each batch is `DELETE … WHERE id IN (SELECT id … WHERE expires_at <= :t ORDER BY expires_at LIMIT
:n)`.** Postgres has no `DELETE … LIMIT`; the subquery bounds the statement, seeking the table's
`ix_<table>_expires_at`, so a large backlog is many short statements rather than one long one that
holds row locks across the table. **Inclusive `<=`**, as every aggregate's `is_expired` is: a row at
exactly `expires_at` is already refused by every code path, so it is already the sweep's.

**No user lock is taken** (§0.8). A `ResetPassword` holding a reset row `FOR UPDATE` makes this
statement wait on that row; if it deletes the row first, the sweep's `DELETE` re-checks the locked
row under READ COMMITTED, finds it gone, and deletes nothing for it. A login's retired hashes go by
the `identity_retired_refresh_token.login_id` cascade.

**Nothing here commits and nothing logs.** `CommittingExpiredIdentityTokens`
(`infrastructure/retention/data_access.py`) commits each batch, which is the port's "durable on
return"; the one line per sweep is the task's, built from the report the use case returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Table, delete, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.infrastructure.persistence.mapping.identity.login import login_table
from tailorcraft.infrastructure.persistence.mapping.identity.password_reset import (
    password_reset_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.pending_registration import (
    pending_registration_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import ExpiredIdentityTokenPort

# The three tables the sweep drains, in the order the use case drains them.
_SWEPT: tuple[Table, ...] = (pending_registration_table, password_reset_table, login_table)


class SqlAlchemyExpiredIdentityTokens:
    """Everything the identity token sweep asks of the store, over one `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_overdue(self, as_of: datetime, grace: timedelta) -> int:
        """`count(*)` of rows with `expires_at <= as_of - grace`, across the three tables — one
        statement, three index range scans, no lock (a report that blocks a login is worse than one
        that is a moment stale). Inclusive, as the port says ("at or before")."""
        cutoff = as_of - grace
        per_table = union_all(
            *(
                select(func.count().label("n")).where(table.c.expires_at <= cutoff)
                for table in _SWEPT
            )
        ).subquery()
        result = await self._session.execute(select(func.coalesce(func.sum(per_table.c.n), 0)))
        return int(result.scalar_one())

    async def delete_expired_pending(self, as_of: datetime, limit: int) -> int:
        return await self._delete_batch(pending_registration_table, as_of, limit)

    async def delete_expired_resets(self, as_of: datetime, limit: int) -> int:
        return await self._delete_batch(password_reset_table, as_of, limit)

    async def delete_expired_logins(self, as_of: datetime, limit: int) -> int:
        return await self._delete_batch(login_table, as_of, limit)

    async def _delete_batch(self, table: Table, as_of: datetime, limit: int) -> int:
        """One bounded `DELETE`; the statement's own row count, read on the session's connection."""
        batch = (
            select(table.c.id)
            .where(table.c.expires_at <= as_of)
            .order_by(table.c.expires_at)
            .limit(limit)
        )
        connection = await self._session.connection()
        result = await connection.execute(delete(table).where(table.c.id.in_(batch)))
        return result.rowcount


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_expired_identity_tokens(
        adapter: SqlAlchemyExpiredIdentityTokens,
    ) -> None:
        _: ExpiredIdentityTokenPort = adapter
