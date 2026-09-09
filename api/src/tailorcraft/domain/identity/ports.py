"""Ports the `identity` context needs from the outside world, in the domain's own language.

`GuestSessionRepository` names no library, no HTTP detail, no cookie, no hashing algorithm — those
are `infrastructure/api/guest_session.py`'s business (ADR-0010). Implemented in
`infrastructure/persistence/repositories/identity/guest_session.py`; neither module is imported here.
"""

from __future__ import annotations

from typing import Protocol

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId


class GuestSessionRepository(Protocol):
    """Persistence for the `GuestSession` aggregate."""

    def next_identity(self) -> GuestSessionId:
        """Mint an id for a `GuestSession` that does not exist yet. Synchronous for the same reason
        `BaseCvRepository.next_identity` is (`domain/intake/ports.py`): application-assigned UUIDv7
        needs no I/O."""
        ...

    async def add(self, session: GuestSession) -> None: ...

    async def get(self, session_id: GuestSessionId) -> GuestSession:
        """Raises `GuestSessionNotFound` if no `GuestSession` with this id exists.

        `get`, not `find` — used by `UploadBaseCv` (technical-plan.md step 1), which is handed a
        `GuestSessionId` it already resolved from a valid cookie in this same request; if the row is
        gone by the time the use case runs, that is exceptional (the session expired *and* was
        purged, or the id is stale), not an ordinary branch the use case is expected to handle. See
        `find_by_token_hash` below for the "absence is ordinary" counterpart.
        """
        ...

    async def find_by_token_hash(self, token_hash: str) -> GuestSession | None:
        """Look up a session by the hash of whatever cookie arrived on the request. Returns `None`
        rather than raising when there is no match, because "no such session" — a missing cookie, an
        expired one already purged, a forged value — is the ordinary shape of an anonymous or expired
        visitor and every caller must handle it, not the exceptional case `get` above models."""
        ...
