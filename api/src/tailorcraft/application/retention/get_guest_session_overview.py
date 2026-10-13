"""The `GetGuestSessionOverview` use case: one live guest session and what it owns (slice 4.4,
AC-5, ADR-0035).

Absent and expired are **one** answer, `GuestSessionNotFound`: an admin cannot tell "never existed"
from "expired" from "claimed", and need not. Read-only; takes no lock.
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.guest_sessions import (
    GuestSessionDirectoryQuery,
    GuestSessionSummary,
)
from tailorcraft.domain.shared.clock import Clock


class GetGuestSessionOverview:
    """Flow of `__call__`: `directory.get(session_id, as_of=clock.now())`; `None` →
    `GuestSessionNotFound` → the summary."""

    def __init__(self, directory: GuestSessionDirectoryQuery, clock: Clock) -> None:
        self._directory = directory
        self._clock = clock

    async def __call__(self, session_id: GuestSessionId) -> GuestSessionSummary:
        raise NotImplementedError
