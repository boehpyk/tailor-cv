"""`SqlAlchemyBaseCvRepository` — the `BaseCvRepository` port (ADR-0007).

Filters below query `BaseCv._id` / `BaseCv._guest_session_id`, the **private** attributes the
imperative mapping in `infrastructure/persistence/mapping/intake/base_cv.py` targets — never
`BaseCv.id` / `BaseCv.guest_session_id`. Those short names are plain read-only `@property` objects on
the domain class, not `InstrumentedAttribute`s: `select(BaseCv).where(BaseCv.id == x)` would call the
property, get back a `BaseCvId`, evaluate a bare Python `==` against `x`, and build
`select(...).where(True)` or `.where(False)` — a predicate that matches everything or nothing,
silently, with no error anywhere. It looks like a typo the first time you see it; it is not one. See
the identical note in `repositories/identity/guest_session.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.infrastructure.identifiers import uuid7

if TYPE_CHECKING:
    from tailorcraft.domain.intake.ports import BaseCvRepository

# `BaseCv._id` etc. are class-body annotations only (`domain/intake/base_cv.py` sets no value —
# `map_imperatively`'s `properties=` installs the real `InstrumentedAttribute` at import time), so
# mypy sees them typed as the *domain* type (`BaseCvId`, `datetime`, …), not as SQLAlchemy's mapped
# attribute. `BaseCv._id == cv_id` then type-checks as `BaseCvId.__eq__`, returning a plain `bool` —
# which is exactly the runtime trap the module docstring describes, just caught by mypy instead of a
# silently-empty query. These `cast`s tell mypy what is actually there at runtime without touching
# behaviour; each is a `cast`, not an `Any`, so CLAUDE.md's ban on unjustified `Any` does not apply.
_BASE_CV_ID: InstrumentedAttribute[BaseCvId] = cast("InstrumentedAttribute[BaseCvId]", BaseCv._id)
_BASE_CV_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId] = cast(
    "InstrumentedAttribute[GuestSessionId]", BaseCv._guest_session_id
)
_BASE_CV_UPLOADED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", BaseCv._uploaded_at
)


class SqlAlchemyBaseCvRepository:
    """Persistence for `BaseCv`, backed by `intake_base_cv`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern, opened once per request in
    `infrastructure/api/deps.py::get_session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> BaseCvId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return BaseCvId(uuid7())

    async def add(self, cv: BaseCv) -> None:
        self._session.add(cv)
        # `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        # task), not to the repository.
        await self._session.flush()

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        # The column stays on the left of `==` below (silencing ruff's SIM300 "Yoda condition"):
        # see the module-level cast comment above for why swapping it to satisfy that check would
        # silently re-break mypy.
        result = await self._session.execute(
            select(BaseCv).where(_BASE_CV_ID == cv_id)  # noqa: SIM300
        )
        found = result.scalar_one_or_none()
        if found is None:
            raise BaseCvNotFound(f"no BaseCv with id {cv_id!r}")
        return found

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        """Newest first — the sensible default for a UI list (`GET /api/base-cvs`): a guest who has
        just uploaded a second CV expects to see it above the first, not have to scroll for it."""
        result = await self._session.execute(
            select(BaseCv)
            .where(_BASE_CV_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_BASE_CV_UPLOADED_AT.desc())
        )
        return result.scalars().all()

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """`SELECT count(*)`, not `len(await list_for_session(sid))` — the port docstring is explicit
        that this must not materialize every row just to measure them."""
        result = await self._session.execute(
            select(func.count()).select_from(BaseCv).where(_BASE_CV_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyBaseCvRepository` structurally satisfies `BaseCvRepository` rather
    # than trusting the shape by eye. Never executed — a `Protocol` needs no instance to check
    # against, only a compatible signature — so it costs nothing at runtime.
    def _assert_implements_base_cv_repository(repo: SqlAlchemyBaseCvRepository) -> None:
        _: BaseCvRepository = repo
