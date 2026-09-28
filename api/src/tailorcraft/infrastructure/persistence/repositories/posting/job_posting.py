"""`SqlAlchemyJobPostingRepository` — the `JobPostingRepository` port (ADR-0007).

Filters below query `JobPosting._id` / `JobPosting._owner_guest_session_id`, the **private**
attributes the imperative mapping targets — never `JobPosting.id` / `JobPosting.owner`. Those short names
are plain read-only `@property` objects on the domain class, not `InstrumentedAttribute`s:
`select(JobPosting).where(JobPosting.id == x)` would call the property, get back a `JobPostingId`,
evaluate a bare Python `==` against `x`, and build `select(...).where(True)` or `.where(False)` — a
predicate that matches everything or nothing, silently, with no error anywhere. It looks like a typo
the first time you see it; it is not one. See the identical note in `repositories/intake/base_cv.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, assert_never, cast

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint

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
_JOB_POSTING_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId | None] = cast(
    "InstrumentedAttribute[GuestSessionId | None]", JobPosting._owner_guest_session_id
)
_JOB_POSTING_USER_ID: InstrumentedAttribute[UserId | None] = cast(
    "InstrumentedAttribute[UserId | None]", JobPosting._owner_user_id
)
_JOB_POSTING_CREATED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", JobPosting._created_at
)

# Recognised by name, never by message (`violated_constraint`). Renaming the FK in
# `mapping/posting/job_posting.py` is a breaking change to `add` below.
_USER_FK: Final = "fk_posting_job_posting_user_id_identity_user"


def _owned_by(owner: Owner) -> ColumnElement[bool]:
    """The owner as a `WHERE` clause: the variant picks the column, the CHECK implies the other.

    `IS NULL` on the other column is not added, and not needed: `ck_posting_job_posting_exactly_one_owner`
    guarantees a row matching one owner column has the other empty.
    """
    match owner:
        case GuestOwner(guest_session_id=guest_session_id):
            return _JOB_POSTING_GUEST_SESSION_ID == guest_session_id  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        case UserOwner(user_id=user_id):
            return _JOB_POSTING_USER_ID == user_id  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        case _:
            assert_never(owner)


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
        """Insert `posting`; raise `UserNotFound` if its `UserOwner` no longer exists (H-53).

        `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request), not
        to the repository.

        **The race this translates** is `SqlAlchemyBaseCvRepository.add`'s (2.2's S-12), one table
        over: a capture resolves the user, then inserts; an account erasure that lands in between
        holds the `identity_user` row `FOR UPDATE`, this `INSERT`'s FK check waits for `FOR KEY
        SHARE` on it, and once the erasure commits the FK refuses. That refusal *is* "the user is
        gone", so it becomes the same error `resolve_existing_user` raises, and the same 401.

        **Inside a SAVEPOINT**, for the reason given there: a failed flush at the root expires every
        instance in the session inside the flush (the 1.4 lesson); a SAVEPOINT confines it to this
        one pending posting. Any other refusal — the guest FK, a CHECK — is not "the user is gone"
        and propagates untranslated.
        """
        # Read before the flush: after a nested rollback the pending posting is expunged.
        posting_id = posting.id
        try:
            async with self._session.begin_nested():
                self._session.add(posting)
                await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint(exc) == _USER_FK:
                # `from None`: the listener already reduced the chain to identifiers, and the frame
                # holds `posting`, whose text names a job someone is applying for (Constitution §8).
                raise UserNotFound(f"the owner of {posting_id!r} no longer exists") from None
            raise

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

    async def count_for_owner(self, owner: Owner) -> int:
        """`SELECT count(*)` over the owner's column — `count_for_session`'s reason, either variant.

        A guest's count seeks `ix_posting_job_posting_guest_session_id`; a user's, the leading
        column of `ix_posting_job_posting_user_id_created_at`.
        """
        result = await self._session.execute(
            select(func.count()).select_from(JobPosting).where(_owned_by(owner))
        )
        return result.scalar_one()

    async def list_recent_for_user(self, user_id: UserId, limit: int) -> Sequence[JobPosting]:
        """Newest first, at most `limit` — `ORDER BY created_at DESC, id DESC LIMIT :limit`.

        The order is `ix_posting_job_posting_user_id_created_at`'s exactly, so this is a range scan
        that stops after `limit` rows: the texts it loads are the ones returned, never the user's
        whole collection. `id DESC` breaks a whole-second tie the way `list_for_session`'s run
        twin does — a UUIDv7's byte order is time order one resolution down.
        """
        result = await self._session.execute(
            select(JobPosting)
            .where(_JOB_POSTING_USER_ID == user_id)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_JOB_POSTING_CREATED_AT.desc(), _JOB_POSTING_ID.desc())
            .limit(limit)
        )
        return result.scalars().all()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyJobPostingRepository` structurally satisfies
    # `JobPostingRepository` rather than trusting the shape by eye. Never executed — a `Protocol`
    # needs no instance to check against, only a compatible signature — so it costs nothing at
    # runtime. This is also what `domain/posting/ports.py` points at when it explains why a Protocol
    # gets no red-first cycle of its own.
    def _assert_implements_job_posting_repository(repo: SqlAlchemyJobPostingRepository) -> None:
        _: JobPostingRepository = repo
