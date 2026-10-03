"""The one-time-token repositories' committing wrappers (slice 2.5, technical plan §0.4, §3).

`claim_access.py`'s `CommittingGuestWorkClaim` for the two aggregates whose writes the ports promise
are **durable on return**, and for the same reason: `application/` may not name a commit (ADR-0002),
so the transaction boundary those promises need is this file's.

- **Request path** (`RequestRegistration`, `RequestPasswordReset`): `put` / `add`, then enqueue. The
  worker must never be handed an id it cannot yet see, so the write commits before the enqueue.
- **Delivery path** (the two `Deliver…Mail` use cases, in the worker): `save_issued` is §0.4's
  *commit, then send* — a crash between the two leaves no mail rather than a mailed link that resolves
  to nothing — and `remove` (expired, account exists, recipient rejected) is final before the task
  returns.

**What commits and what does not.** Every *write* the use cases make through these ports commits;
every read passes through, and a locking read (`lock_by_token_hash`) must not commit — that would
release the lock it exists to take. `PasswordResetRepository.remove_all_for_user` passes through:
it runs inside `ResetPassword`'s transaction beside the user's new hash, and committing it alone would
split the two.

**No commit when the inner write raises** (`…AlreadyIssued`, a database error): nothing was written,
and the caller's session ends the transaction. Committing in a `finally` would be the bug.

Delegation, not a subclass, for the import-order reason `CommittingExpiredGuestDataAdapter` gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.ports import (
    PasswordResetRepository,
    PendingRegistrationRepository,
)
from tailorcraft.domain.identity.value_objects import (
    PasswordResetId,
    PendingRegistrationId,
    TokenHash,
    UserId,
)


class CommittingPendingRegistrationRepository:
    """`PendingRegistrationRepository` whose `put`, `save_issued` and `remove` commit."""

    def __init__(self, inner: PendingRegistrationRepository, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    def next_identity(self) -> PendingRegistrationId:
        return self._inner.next_identity()

    async def put(self, pending: PendingRegistration) -> None:
        """The upsert, **then commit** — before `RequestRegistration` enqueues the id."""
        await self._inner.put(pending)
        await self._session.commit()

    async def get(self, pending_id: PendingRegistrationId) -> PendingRegistration | None:
        """A read, so no commit."""
        return await self._inner.get(pending_id)

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PendingRegistration | None:
        """A read that takes the lock, so **no commit**."""
        return await self._inner.lock_by_token_hash(token_hash)

    async def save_issued(self, pending: PendingRegistration) -> None:
        """The guarded `UPDATE`, **then commit** — the *commit* in *commit, then send*."""
        await self._inner.save_issued(pending)
        await self._session.commit()

    async def remove(self, pending_id: PendingRegistrationId) -> None:
        await self._inner.remove(pending_id)
        await self._session.commit()


class CommittingPasswordResetRepository:
    """`PasswordResetRepository` whose `add`, `save_issued` and `remove` commit."""

    def __init__(self, inner: PasswordResetRepository, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    def next_identity(self) -> PasswordResetId:
        return self._inner.next_identity()

    async def add(self, reset: PasswordReset) -> None:
        """The insert, **then commit** — before `RequestPasswordReset` enqueues the id."""
        await self._inner.add(reset)
        await self._session.commit()

    async def get(self, reset_id: PasswordResetId) -> PasswordReset | None:
        """A read, so no commit."""
        return await self._inner.get(reset_id)

    async def find_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """A read, so no commit."""
        return await self._inner.find_by_token_hash(token_hash)

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """A read that takes the lock, so **no commit**."""
        return await self._inner.lock_by_token_hash(token_hash)

    async def save_issued(self, reset: PasswordReset) -> None:
        """The issue and the supersede, **then commit** — one unit, before the mail is sent."""
        await self._inner.save_issued(reset)
        await self._session.commit()

    async def remove(self, reset_id: PasswordResetId) -> None:
        await self._inner.remove(reset_id)
        await self._session.commit()

    async def remove_all_for_user(self, user_id: UserId) -> int:
        """Passes through **without** a commit — see the module docstring."""
        return await self._inner.remove_all_for_user(user_id)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance here, where it is defined. Never executed.
    def _assert_implements_ports(
        pending: CommittingPendingRegistrationRepository,
        resets: CommittingPasswordResetRepository,
    ) -> None:
        _p: PendingRegistrationRepository = pending
        _r: PasswordResetRepository = resets
