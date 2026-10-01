"""The claim's committing wrapper (slice 2.4, ADR-0025, technical plan §3).

`CommittingGuestWorkClaim` is `infrastructure/retention/data_access.py`'s `CommittingAccountData`
for the claim, and for the same sentence: *"rows first, **committed**, then files"*. `ClaimGuestWork`
unlinks the dropped working copies' files after `transfer` returns, and a crash between the two must
leave orphan files for the sweep — never a row pointing at bytes that are gone. Only a commit makes
that true, and `application/` may not name one (ADR-0002).

It lives in `identity` because the claim does (ADR-0025 decision 8). Not a domain port: a transaction
boundary is a persistence-boundary decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ports import GuestWorkClaimPort
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId


class CommittingGuestWorkClaim:
    """The claim's `GuestWorkClaimPort`: the ordinary one, except that **`transfer` commits.**

    **The commit is what makes the returned `ClaimedGuestWork` a fact** (the port's promise: "rows
    only, durable on return"). It is also what releases the session row's `FOR UPDATE`, at the only
    right moment: a second claim or the purge waiting on it then finds the row gone (AC-14, AC-16),
    and a guest `INSERT` waiting on its FK check fails against a committed deletion (AC-17).

    **No commit when `transfer` raises.** A `UserNotFound` (the account was erased first) or a
    database error leaves the transaction for the caller to roll back, and every row still
    guest-owned. Committing in a `finally` would be the bug.

    **`lock_session` does not commit**: committing there would release the lock before the
    transfer, which is the race the lock exists to close. On the paths that never transfer (no such
    session, expired), the router's rollback ends the transaction and releases the lock.

    **No `expunge` before the inner statements**, for `CommittingExpiredGuestDataAdapter`'s reason:
    the claim's route writes through no repository on this session, so nothing is dirty for an
    autoflush to push out; the one aggregate loaded (`GuestSession`) is expunged by the inner adapter
    before its row is deleted.

    Delegation, not a subclass, for the import-order reason `CommittingExpiredGuestDataAdapter`
    gives: the adapter module reads mapped tables at import time.
    """

    def __init__(self, inner: GuestWorkClaimPort, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    async def lock_session(self, token_hash: str) -> GuestSession | None:
        """A read that takes the lock, so **no commit**."""
        return await self._inner.lock_session(token_hash)

    async def transfer(self, session_id: GuestSessionId, user_id: UserId) -> ClaimedGuestWork:
        """The inner six statements, **then commit** — the whole reason this class exists."""
        claimed = await self._inner.transfer(session_id, user_id)
        await self._session.commit()
        return claimed


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance here, where it is defined. Never executed.
    def _assert_implements_guest_work_claim(adapter: CommittingGuestWorkClaim) -> None:
        _: GuestWorkClaimPort = adapter
