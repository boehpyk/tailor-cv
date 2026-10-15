"""`SqlGuestSessionDirectory` — the `GuestSessionDirectoryQuery` port (slice 4.4, ADR-0035; ADR-0024).

Every **live** guest session, one keyset page at a time, in **one statement** (technical plan §3):

    SELECT id, created_at, expires_at,
           (SELECT count(*) FROM intake_base_cv      WHERE guest_session_id = s.id),
           (SELECT count(*) FROM posting_job_posting WHERE guest_session_id = s.id),
           (SELECT count(*) FROM tailoring_run       WHERE guest_session_id = s.id),
           (SELECT count(*) FROM export_job          WHERE guest_session_id = s.id)
    FROM identity_guest_session s
    WHERE expires_at > :as_of
      AND (expires_at, id) < (:cursor_expires_at, :cursor_id)   -- only when a cursor is given
    ORDER BY expires_at DESC, id DESC
    LIMIT :limit + 1

**Never `token_hash`** (AC-28): the columns are named, and that one is the only column on the table
that could authenticate a request. Nothing from the four child tables but a count either — no file
key, no filename, no text a stranger typed.

**Correlated counts, not `LEFT JOIN … GROUP BY`**: four joins on one session multiply each other's
rows, and each subquery is one probe of the child table's existing `guest_session_id` index, over at
most 51 sessions a page.

**Keyset, not `OFFSET`**, for history's and the user directory's reasons; `id` breaks ties on
`expires_at`, which the `Clock` makes whole-second. One extra row says whether a next page exists
and is never returned.

**Nothing here logs.**
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, Select, Table, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.guest_sessions import (
    GuestSessionCursor,
    GuestSessionPage,
    GuestSessionSummary,
)
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Row

    from tailorcraft.domain.retention.guest_sessions import GuestSessionDirectoryQuery

_s = guest_session_table.c


def _count_owned(table: Table) -> ColumnElement[int]:
    return (
        select(func.count())
        .select_from(table)
        .where(table.c.guest_session_id == _s.id)
        .scalar_subquery()
    )


def _summaries(
    as_of: datetime,
) -> Select[tuple[GuestSessionId, datetime, datetime, int, int, int, int]]:
    return select(
        _s.id,
        _s.created_at,
        _s.expires_at,
        _count_owned(base_cv_table).label("base_cvs"),
        _count_owned(job_posting_table).label("job_postings"),
        _count_owned(tailoring_run_table).label("tailoring_runs"),
        _count_owned(export_job_table).label("export_jobs"),
    ).where(_s.expires_at > as_of)


def _summary(
    row: Row[tuple[GuestSessionId, datetime, datetime, int, int, int, int]],
) -> GuestSessionSummary:
    return GuestSessionSummary(
        session_id=row.id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        base_cvs=row.base_cvs,
        job_postings=row.job_postings,
        tailoring_runs=row.tailoring_runs,
        export_jobs=row.export_jobs,
    )


class SqlGuestSessionDirectory:
    """Directory pages over one `AsyncSession`. Reads only; the unit of work is the caller's."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(
        self, as_of: datetime, after: GuestSessionCursor | None, limit: int
    ) -> GuestSessionPage:
        """The port's contract; the statement is the module docstring's."""
        statement = _summaries(as_of).order_by(_s.expires_at.desc(), _s.id.desc()).limit(limit + 1)
        if after is not None:
            # Bound through the columns' own types (`GuestSessionIdType` unwraps the id).
            statement = statement.where(
                tuple_(_s.expires_at, _s.id)
                < tuple_(
                    literal(after.expires_at, _s.expires_at.type),
                    literal(after.session_id, _s.id.type),
                )
            )
        rows = (await self._session.execute(statement)).all()
        items = tuple(_summary(row) for row in rows[:limit])
        next_cursor = (
            GuestSessionCursor(expires_at=items[-1].expires_at, session_id=items[-1].session_id)
            if len(rows) > limit
            else None
        )
        return GuestSessionPage(items=items, next_cursor=next_cursor)

    async def get(self, session_id: GuestSessionId, as_of: datetime) -> GuestSessionSummary | None:
        """Live only: an expired session awaiting the purge is `None`, like a gone one."""
        row = (
            await self._session.execute(_summaries(as_of).where(_s.id == session_id))
        ).one_or_none()
        return None if row is None else _summary(row)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_guest_session_directory(adapter: SqlGuestSessionDirectory) -> None:
        _: GuestSessionDirectoryQuery = adapter
