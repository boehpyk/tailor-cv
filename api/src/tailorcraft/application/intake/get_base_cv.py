"""The `GetBaseCvForSession` use case: read one `BaseCv`, authorized by the link to its session.

This is a use case rather than `cvs.get(base_cv_id)` called straight from a router **because it
carries the authorization rule**, and that rule must not live in a router (technical-plan.md,
"Use cases"; API contract "Auth"):

    What authorizes access to a base CV is **the link** — `cv.guest_session_id == the resolved
    session id` — checked here, on every read. Owning a session id is not authority over an object
    that references it (ADR-0008): a guest session is not a login, and the id itself proves nothing
    about which rows it may see.

The check lives in the use case, not the router or a dependency, so that a **second entry point** —
the Celery task slice 1.5 adds — cannot reach a `BaseCv` without going through the same rule. A
check duplicated in every caller is a check one caller eventually forgets; a check centralized here
is a check every caller gets for free.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.clock import Clock


class GetBaseCvForSession:
    """Look up a `BaseCv` by id, but only if it belongs to `guest_session_id`.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves
    (the same defense-in-depth the API's cookie dependency already applies, repeated here so this
    use case is safe to call from anywhere, not only from behind that dependency).

    Raises `BaseCvNotFound` in **two** distinct situations that must look identical from outside this
    use case: the id does not exist at all, and the id exists but names a `BaseCv` owned by a
    *different* session (F-20/AC-8). This use case never raises `BaseCvNotOwnedBySession` to its
    caller and the API layer never maps a 403 for this endpoint — a distinguishable "wrong owner"
    response would confirm to an attacker that the id exists, which is exactly the information a 404
    is supposed to withhold.

    `BaseCvNotOwnedBySession` (`domain/intake/errors.py`) still exists as a type: this use case's own
    tests need to tell "absent" from "not mine" apart even though the boundary does not, so the
    "not mine" branch is expected to raise `BaseCvNotFound` chained from a `BaseCvNotOwnedBySession`
    (`raise BaseCvNotFound(...) from BaseCvNotOwnedBySession(...)`) — visible to a test inspecting
    `__cause__`, invisible to anything reading only the exception type that crosses this boundary.
    """

    def __init__(
        self,
        cvs: BaseCvRepository,
        sessions: GuestSessionRepository,
        clock: Clock,
    ) -> None:
        self._cvs = cvs
        self._sessions = sessions
        self._clock = clock

    async def __call__(self, base_cv_id: BaseCvId, guest_session_id: GuestSessionId) -> BaseCv:
        raise NotImplementedError
