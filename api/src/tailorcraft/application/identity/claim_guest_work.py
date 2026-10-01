"""The `ClaimGuestWork` use case: a signed-in user takes the work a guest session in the same browser
made, every row of it at once (slice 2.4, ADR-0025, technical plan §2).

It lives in `identity` because a claim is the hand-off between the two principals; `retention`
deletes what an owner has, a claim keeps it. There is no aggregate to load: the invariant (*every row
of a session changes owner together*) spans four contexts' tables and is held by one transaction in
one adapter, behind `GuestWorkClaimPort`. The one domain rule applied here, `GuestSession.is_expired`,
stays on `GuestSession`.

**Why the input is a token *hash*.** The raw cookie never leaves the router; hashing it is
`guest_session.py`'s job, and `GuestSessionRepository.find_by_token_hash(str)` is the precedent.

**Rows first, committed, then files** (ADR-0006 §2). The bound `transfer` commits before it returns,
so every unlink happens after the rows are durable. A failed unlink leaves an orphan for the
operator's sweep; it never fails the claim, because the rows have already moved.

**This layer does not log.** Unlink failures are *returned* by exception type name in the report,
and the entry point decides what reaches a log line — `EraseAccount`'s precedent.

**Idempotent in effect.** The first claim deletes the session row, so every later call with the same
cookie meets `lock_session`'s `None` and reports nothing. The session row is the idempotency key, and
the claim consumes it.

**No command dataclass**: a verified user id and an optional hash, the purge's and `EraseAccount`'s
precedent for inputs with no rules of their own to hold.
"""

from __future__ import annotations

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.domain.identity.claim import GuestWorkClaimReport
from tailorcraft.domain.identity.ports import GuestWorkClaimPort, UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.files import FileStorePort


class ClaimGuestWork:
    """Move everything the guest session behind `token_hash` owns to `user_id`.

    Flow (technical plan §2), in this order:

    1. ``resolve_existing_user(users, user_id)`` — the destination is resolved **first**
       (→ `UserNotFound`), before the claim port is touched at all (§0.3).
    2. ``token_hash is None`` (no guest cookie) → `GuestWorkClaimReport.nothing()`.
    3. ``session = await claims.lock_session(token_hash)``; ``None`` (no such session, already
       purged, or already claimed by a concurrent request that held the lock) → ``nothing()``.
    4. ``session.is_expired(clock.now())`` → ``nothing()``, and ``transfer`` is never called: an
       expired session's work is the purge's, untouched.
    5. ``claimed = await claims.transfer(session.id, user_id)`` — the rows are durable on return.
       A `UserNotFound` from it (the user erased between steps 1 and 5) propagates, and nothing is
       unlinked.
    6. For each ``ref`` in ``claimed.files_to_unlink``: ``await files.delete(ref)``. Any
       `Exception` (`FileStoreUnavailable` is the one `LocalFileStore` documents, but the floor is
       wider on purpose; see the loop) is recorded by its **type name** (never its message, which
       can quote a path) and the loop continues.
    7. Return ``GuestWorkClaimReport.of(claimed, failures)``.
    """

    def __init__(
        self,
        users: UserRepository,
        claims: GuestWorkClaimPort,
        files: FileStorePort,
        clock: Clock,
    ) -> None:
        self._users = users
        self._claims = claims
        self._files = files
        self._clock = clock

    async def __call__(self, user_id: UserId, token_hash: str | None) -> GuestWorkClaimReport:
        await resolve_existing_user(self._users, user_id)
        if token_hash is None:
            return GuestWorkClaimReport.nothing()

        session = await self._claims.lock_session(token_hash)
        if session is None or session.is_expired(self._clock.now()):
            return GuestWorkClaimReport.nothing()

        claimed = await self._claims.transfer(session.id, user_id)

        failures: list[str] = []
        for ref in claimed.files_to_unlink:
            try:
                await self._files.delete(ref)
            except Exception as exc:
                # A floor, not `except FileStoreUnavailable`. `FileStorePort.delete` is a Protocol
                # with no exception list in its signature, so naming the one failure today's adapter
                # translates to is a bet on every future adapter; and C-31's promise is "returned,
                # never raised" because the rows are already committed: an escaping exception would
                # turn a claim that happened into a 500 and skip the remaining unlinks.
                # `EraseAccount` draws the same line for the same reason. `Exception`, never
                # `BaseException`, so a cancellation still cancels. Only the class name is kept.
                failures.append(type(exc).__name__)

        return GuestWorkClaimReport.of(claimed, failures)
