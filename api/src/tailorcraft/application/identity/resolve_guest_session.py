"""`resolve_active_guest_session`: the one "look up the session, refuse it if it is gone or stale"
step every use case that reads on behalf of a guest session needs.

`GetBaseCvForSession` and `ListBaseCvsForSession` (`application/intake/`) both start with the exact
same three lines — resolve the session, raise `GuestSessionNotFound` if the row is gone, raise
`GuestSessionExpired` if `clock.now()` is past `expires_at` — as defense-in-depth against a caller
that reaches a use case without going through the API's own cookie dependency (get_base_cv.py's and
list_base_cvs.py's docstrings both say so explicitly). Two call sites with byte-identical logic is
exactly the case worth factoring: there is no business decision here that could plausibly diverge
between "reading one CV" and "reading all of them," so a shared helper is strictly less code with no
loss of clarity, not a premature abstraction. (`StartGuestSession` does not use this — it *creates* a
session rather than resolving an existing one, so there is nothing here for it to share.)

A plain function rather than a class: it holds no state of its own between calls — every dependency
it needs is already sitting in the caller's `self._sessions` / `self._clock` — so a collaborator
object would only add a constructor that immediately re-forwards to this function's parameters.
"""

from __future__ import annotations

from tailorcraft.domain.identity.errors import GuestSessionExpired
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.clock import Clock


async def resolve_active_guest_session(
    sessions: GuestSessionRepository,
    clock: Clock,
    guest_session_id: GuestSessionId,
) -> GuestSession:
    """Look up `guest_session_id` and confirm it has not expired.

    Raises `GuestSessionNotFound` (from `sessions.get`) if the row does not exist, and
    `GuestSessionExpired` if it exists but `clock.now()` is at or past `expires_at`.
    """
    session = await sessions.get(guest_session_id)
    if session.is_expired(clock.now()):
        raise GuestSessionExpired(str(session.id))
    return session
