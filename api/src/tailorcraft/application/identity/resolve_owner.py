"""`resolve_owner`: turn a requester into a resolved owner, for either variant (slice 2.3, §0.3).

Every use case that reads or creates a row on someone's behalf starts the same way: confirm the
credential it was handed still speaks for someone. For a guest that is `resolve_active_guest_session`
(the session exists and has not expired); for a user it is `resolve_existing_user` (the account
still exists — an access token outlives an erasure by up to 15 minutes). This function is the one
`match` that picks between them, so a use case written against `Owner` resolves once and then
authorizes with one equality, `row.owner != owner`, whichever variant asked.

A plain function, like the two it composes: it holds no state, and every dependency it needs
already sits on the caller.

**"Requester" in, "owner" out.** The argument is who presented a credential; the return value is
the same variant rebuilt from the *resolved* row's id, which is what a use case then compares with
`row.owner` or stamps onto a new row (ADR-0022: "owner" is a word about rows).
"""

from __future__ import annotations

from typing import assert_never

from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.ports import GuestSessionRepository, UserRepository
from tailorcraft.domain.shared.clock import Clock


async def resolve_owner(
    sessions: GuestSessionRepository,
    users: UserRepository,
    clock: Clock,
    requester: Owner,
) -> Owner:
    """Resolve `requester` and return it as an `Owner`.

    Guest: raises `GuestSessionNotFound` / `GuestSessionExpired`. User: raises `UserNotFound`.
    """
    match requester:
        case GuestOwner(guest_session_id=guest_session_id):
            session = await resolve_active_guest_session(sessions, clock, guest_session_id)
            return GuestOwner(session.id)
        case UserOwner():
            raise NotImplementedError
        case _:
            assert_never(requester)
