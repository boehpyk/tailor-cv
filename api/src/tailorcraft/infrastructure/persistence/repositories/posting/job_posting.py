"""`SqlAlchemyJobPostingRepository` — the `JobPostingRepository` port (ADR-0007).

Filters below query `JobPosting._id` / `JobPosting._guest_session_id`, the **private** attributes the
imperative mapping targets — never `JobPosting.id` / `JobPosting.guest_session_id`. Those short names
are plain read-only `@property` objects on the domain class, not `InstrumentedAttribute`s:
`select(JobPosting).where(JobPosting.id == x)` would call the property, get back a `JobPostingId`,
evaluate a bare Python `==` against `x`, and build `select(...).where(True)` or `.where(False)` — a
predicate that matches everything or nothing, silently, with no error anywhere. It looks like a typo
the first time you see it; it is not one. See the identical note in `repositories/intake/base_cv.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.infrastructure.identifiers import uuid7

if TYPE_CHECKING:
    from tailorcraft.domain.posting.ports import JobPostingRepository

# `JobPosting._id` etc. are class-body annotations only (`domain/posting/job_posting.py` assigns no
# value — `map_imperatively`'s `properties=` installs the real `InstrumentedAttribute` at import
# time), so mypy sees them typed as the *domain* type (`JobPostingId`, `datetime`, …) rather than as
# SQLAlchemy's mapped attribute. `JobPosting._id == posting_id` then type-checks as
# `JobPostingId.__eq__` returning a plain `bool` — which is exactly the runtime trap the module
# docstring describes, just caught by mypy instead of by a silently-empty query. These `cast`s tell
# mypy what is actually there at runtime without touching behaviour; each is a `cast`, not an `Any`,
# so CLAUDE.md's ban on unjustified `Any` does not apply.
_JOB_POSTING_ID: InstrumentedAttribute[JobPostingId] = cast(
    "InstrumentedAttribute[JobPostingId]", JobPosting._id
)
_JOB_POSTING_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId] = cast(
    "InstrumentedAttribute[GuestSessionId]", JobPosting._guest_session_id
)
_JOB_POSTING_CREATED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", JobPosting._created_at
)


class SqlAlchemyJobPostingRepository:
    """Persistence for `JobPosting`, backed by `posting_job_posting`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern, opened once per request in
    `infrastructure/api/deps.py::get_session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> JobPostingId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return JobPostingId(uuid7())

    async def add(self, posting: JobPosting) -> None:
        self._session.add(posting)
        # `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request), not
        # to the repository.
        await self._session.flush()

    async def get(self, posting_id: JobPostingId) -> JobPosting:
        # The column stays on the left of `==` below (silencing ruff's SIM300 "Yoda condition"):
        # see the module-level cast comment for why swapping it to satisfy that check would
        # silently re-break mypy.
        result = await self._session.execute(
            select(JobPosting).where(_JOB_POSTING_ID == posting_id)  # noqa: SIM300
        )
        found = result.scalar_one_or_none()
        if found is None:
            raise JobPostingNotFound(f"no JobPosting with id {posting_id!r}")
        return found

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[JobPosting]:
        """Newest first — a guest who has just captured a second posting expects to see it above the
        first rather than scroll for it."""
        result = await self._session.execute(
            select(JobPosting)
            .where(_JOB_POSTING_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_JOB_POSTING_CREATED_AT.desc())
        )
        return result.scalars().all()

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """`SELECT count(*)`, not `len(await list_for_session(sid))` — the port docstring is explicit
        that this must not materialize every row just to measure them, and here each row carries up
        to 30,000 characters of posting text."""
        result = await self._session.execute(
            select(func.count()).select_from(JobPosting).where(_JOB_POSTING_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyJobPostingRepository` structurally satisfies
    # `JobPostingRepository` rather than trusting the shape by eye. Never executed — a `Protocol`
    # needs no instance to check against, only a compatible signature — so it costs nothing at
    # runtime. This is also what `domain/posting/ports.py` points at when it explains why a Protocol
    # gets no red-first cycle of its own.
    def _assert_implements_job_posting_repository(repo: SqlAlchemyJobPostingRepository) -> None:
        _: JobPostingRepository = repo
