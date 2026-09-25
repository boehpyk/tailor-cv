"""`CommittingBaseCvRemoval` — the delete route's `BaseCvRepository` (slice 2.2, technical plan §0.4).

`DeleteSavedBaseCv` removes a saved CV's row and only then unlinks its file, so that a crash between
the two leaves an orphan file the sweep reclaims rather than a row pointing at nothing (ADR-0006 §2).
That order is a statement about **durability**, and it is only true if the `DELETE` has committed
before the `unlink` runs. The use case cannot say so — `BaseCvRepository.remove` names no
transaction and `application/` may not name one (ADR-0002) — so the boundary is this class's, in the
composition root, exactly as `CommittingExpiredGuestDataAdapter` is the purge's
(`infrastructure/retention/data_access.py`).

It lives in `infrastructure/intake/` rather than `persistence/`, beside that precedent's reasoning:
it is not an adapter to a store but a decision about *when* one becomes durable, made for one route.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId


class CommittingBaseCvRemoval:
    """The ordinary `BaseCvRepository`, except that **`remove` commits.**

    **Only `remove`.** Every other method passes straight through: reads need no commit, and the
    delete route writes nothing else — the handler's own commit (FastAPI 0.141 runs a dependency's
    teardown after the response, so a response that reflects a write commits inside the handler)
    remains the boundary for anything else a root ever routes through here.

    **Delegation rather than a subclass of `SqlAlchemyBaseCvRepository`**, for the import-order reason
    `CommittingTailoringRunRepository` gives: that module reads `BaseCv._id` and friends at import
    time to build its `InstrumentedAttribute` casts, which exist only after `configure_mappings()`,
    and a `class X(SqlAlchemyBaseCvRepository)` statement is a top-level import by another name. The
    pass-throughs are the price; `mypy --strict` checking this against the Protocol is what keeps them
    from drifting.

    **Commit after a refusal? No.** A zero-row `remove` raises `BaseCvNotFound` out of the inner call,
    so the commit below never runs, and nothing was written to commit.
    """

    def __init__(self, inner: BaseCvRepository, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    def next_identity(self) -> BaseCvId:
        return self._inner.next_identity()

    async def add(self, cv: BaseCv) -> None:
        await self._inner.add(cv)

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        return await self._inner.get(cv_id)

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        return await self._inner.list_for_session(sid)

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return await self._inner.count_for_session(sid)

    async def list_for_user(self, uid: UserId) -> Sequence[BaseCv]:
        return await self._inner.list_for_user(uid)

    async def count_for_user(self, uid: UserId) -> int:
        return await self._inner.count_for_user(uid)

    async def save_label(self, cv: BaseCv) -> None:
        await self._inner.save_label(cv)

    async def remove(self, cv_id: BaseCvId, owner: UserOwner) -> None:
        """The inner `DELETE`, **then commit** — the whole reason this class exists.

        The commit is what `DeleteSavedBaseCv`'s unlink, which follows this call, is allowed to rely
        on (AC-27 checks it from a separate connection at the moment `delete` is called). It is safe
        under the aggregate the use case still holds because the inner `remove` expunged it first and
        `create_session_factory` sets `expire_on_commit=False`: nothing is flushed, nothing expired.
        """
        await self._inner.remove(cv_id, owner)
        await self._session.commit()


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance rather than trusting nine signatures by eye.
    def _assert_implements_base_cv_repository(repo: CommittingBaseCvRemoval) -> None:
        _: BaseCvRepository = repo
