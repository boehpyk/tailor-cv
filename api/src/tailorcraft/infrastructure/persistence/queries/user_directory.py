"""`SqlAlchemyUserDirectoryQuery` — the `UserDirectoryQuery` port (slice 4.2, ADR-0033; ADR-0024).

Every registered account, one keyset page at a time, in **one statement** (technical plan §3):

    SELECT id, email, role, created_at, disabled_at
    FROM identity_user
    WHERE strpos(email, :term) > 0                          -- only when a search is given
      AND (created_at, id) < (:cursor_at, :cursor_id)       -- only when a cursor is given
    ORDER BY created_at DESC, id DESC
    LIMIT :limit + 1

**Never `password_hash`** (AC-15): the columns are named, and a directory row is not a `User`.

**`strpos`, not `LIKE`.** A search is a literal substring: `%`, `_` and `\\` mean themselves, so a
search cannot widen itself into "every row" and there is nothing to escape. Both sides are already
lower-case: `EmailSearch` refuses anything its normalization would change, and
`ck_identity_user_email_normalized` holds the column to `lower(btrim(email))`.

**Keyset, not `OFFSET`**, for history's reasons: a row-value comparison on `(created_at, id)`
neither skips nor repeats a row when an account is added between two page loads, and `id` breaks
ties because the `Clock` is whole-second. One extra row is fetched to know whether a next page
exists; it is never returned. **No index** (OQ-9): the sort is over at most the user count.

**Rows rehydrate through the mapping's `TypeDecorator`s** (`UserIdType`, `EmailAddressType`,
`RoleType`), so a bad stored email or role fails loudly instead of reaching an administrator's
screen. At most 51 rows a page, so the per-row cost is nothing like 2.2's `ExtractedText` case.

**Nothing here logs**, and an email never would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.user_directory import (
    EmailSearch,
    UserDirectoryCursor,
    UserDirectoryEntry,
    UserDirectoryPage,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table

if TYPE_CHECKING:
    from tailorcraft.domain.identity.user_directory import UserDirectoryQuery

_u = user_table.c


class SqlAlchemyUserDirectoryQuery:
    """Directory pages over one `AsyncSession`. Reads only; the unit of work is the caller's."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(
        self, search: EmailSearch | None, after: UserDirectoryCursor | None, limit: int
    ) -> UserDirectoryPage:
        """The port's contract; the statement is the module docstring's."""
        statement = (
            select(_u.id, _u.email, _u.role, _u.created_at, _u.disabled_at)
            .order_by(_u.created_at.desc(), _u.id.desc())
            .limit(limit + 1)
        )
        if search is not None:
            statement = statement.where(func.strpos(_u.email, literal(search.value, String())) > 0)
        if after is not None:
            # Bound through the columns' own types (`UserIdType` unwraps the id), as history does.
            statement = statement.where(
                tuple_(_u.created_at, _u.id)
                < tuple_(
                    literal(after.created_at, _u.created_at.type),
                    literal(after.user_id, _u.id.type),
                )
            )

        rows = (await self._session.execute(statement)).all()
        entries = tuple(
            UserDirectoryEntry(
                id=row.id,
                email=row.email,
                role=row.role,
                created_at=row.created_at,
                disabled_at=row.disabled_at,
            )
            for row in rows[:limit]
        )
        next_cursor = (
            UserDirectoryCursor(created_at=entries[-1].created_at, user_id=entries[-1].id)
            if len(rows) > limit
            else None
        )
        return UserDirectoryPage(entries=entries, next_cursor=next_cursor)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_user_directory_query(adapter: SqlAlchemyUserDirectoryQuery) -> None:
        _: UserDirectoryQuery = adapter
