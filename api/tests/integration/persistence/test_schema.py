"""Schema-level persistence tests that don't belong to either aggregate's repository module: the
AC-14 cascade delete and `ix_intake_base_cv_guest_session_id` (the sibling of
`test_guest_session_repository.py`'s `ix_identity_guest_session_expires_at` check).

Both indexes serve slice 1.6's purge before that slice exists (technical-plan.md's "Data &
migrations"): `ix_identity_guest_session_expires_at` for `WHERE expires_at < now()`, and this
module's `ix_intake_base_cv_guest_session_id` for the cascade delete itself, `GET /api/base-cvs`, and
`count_for_session`. Asserting both now is what stops either being quietly dropped by a future
migration edit that "cleans up" an index nothing in *this* slice's queries appears to use.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)


async def test_guest_session_id_index_exists_on_base_cv(session: AsyncSession) -> None:
    """Serves `GET /api/base-cvs`, `count_for_session`, and the cascade delete below."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'intake_base_cv' "
            "AND indexname = 'ix_intake_base_cv_guest_session_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_intake_base_cv_guest_session_id"


async def test_deleting_a_guest_session_cascades_to_its_base_cvs(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-14: `intake_base_cv.guest_session_id` is a `NOT NULL` FK with `ON DELETE CASCADE`, so
    deleting a session row removes every `BaseCv` it owns in one statement — no join the 1.6 purge
    job could get wrong.

    The delete is issued as a raw statement against `guest_session_table`, not through
    `GuestSession`/`SqlAlchemyGuestSessionRepository` (this slice ships no delete path — the purge
    job is 1.6): the point of this test is what the *database* does when a session row disappears,
    independent of which future caller triggers it.
    """
    sessions = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=sessions.next_identity(), token_hash="f" * 64, at=clock.now(), ttl_hours=24
    )
    await sessions.add(owner)

    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        guest_session_id=owner.id,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(cv)

    # Columns are `TypeDecorator`-backed (`GuestSessionIdType` / `BaseCvIdType`), so the bound
    # parameter has to be the domain value object the decorator's `process_bind_param` expects
    # (`owner.id`, `cv_id`) — not the bare `UUID` underneath it.
    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    remaining = await session.execute(select(base_cv_table.c.id).where(base_cv_table.c.id == cv_id))
    assert remaining.scalar_one_or_none() is None
