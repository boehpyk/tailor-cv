"""The `ListLiveGuestSessions` use case: one keyset page of the administrator's guest-session
directory (slice 4.4, AC-4, ADR-0035).

4.2's `ListUsers` shape: a pass-through to `GuestSessionDirectoryQuery`, so the router depends on a
use case like every other route, and the page-size bound (1..`PAGE_SIZE_MAX`) is asserted in one
place. "Live" is judged at **one** instant, the clock's. Read-only; takes no lock.
"""

from __future__ import annotations

from tailorcraft.domain.retention.guest_sessions import (
    GuestSessionCursor,
    GuestSessionDirectoryQuery,
    GuestSessionPage,
)
from tailorcraft.domain.shared.clock import Clock


class ListLiveGuestSessions:
    """Flow of `__call__`: `limit` outside 1..`PAGE_SIZE_MAX` → `InvariantViolated` → `page(as_of=
    clock.now(), after, limit)`, returned unchanged."""

    def __init__(self, directory: GuestSessionDirectoryQuery, clock: Clock) -> None:
        self._directory = directory
        self._clock = clock

    async def __call__(self, after: GuestSessionCursor | None, limit: int) -> GuestSessionPage:
        raise NotImplementedError
