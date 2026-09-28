"""The `GetBaseCv` use case: read one `BaseCv`, authorized by the link to its owner.

Renamed from 1.1's `GetBaseCv` in slice 2.3 (§0.3): it takes a `requester: Owner`, so one
use case serves the guest workspace and a signed-in user's.

This is a use case rather than `cvs.get(base_cv_id)` called straight from a router **because it
carries the authorization rule**, and that rule must not live in a router (technical-plan.md,
"Use cases"; API contract "Auth"):

    What authorizes access to a base CV is **the link** — `cv.owner == GuestOwner(the resolved
    session id)` — checked here, on every read. Owning a session id is not authority over an object
    that references it (ADR-0008): a guest session is not a login, and the id itself proves nothing
    about which rows it may see.

The check lives in the use case, not the router or a dependency, so that a **second entry point** —
the Celery task slice 1.5 adds — cannot reach a `BaseCv` without going through the same rule. A
check duplicated in every caller is a check one caller eventually forgets; a check centralized here
is a check every caller gets for free.
"""

from __future__ import annotations

from typing import assert_never

from tailorcraft.application.identity.resolve_owner import resolve_owner
from tailorcraft.application.intake.owned_saved_base_cv import get_owned_saved_base_cv
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.ports import GuestSessionRepository, UserRepository
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedBySession
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.clock import Clock


class GetBaseCv:
    """Look up a `BaseCv` by id, but only if it belongs to `requester`.

    Guest arm: 1.1's rule, below. User arm: 2.2's `get_owned_saved_base_cv`, with
    `BaseCvNotOwnedByUser` as the `__cause__`; raises `UserNotFound` if the account is gone.

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
        users: UserRepository,
        clock: Clock,
    ) -> None:
        self._cvs = cvs
        self._sessions = sessions
        self._users = users
        self._clock = clock

    async def __call__(self, base_cv_id: BaseCvId, requester: Owner) -> BaseCv:
        owner = await resolve_owner(self._sessions, self._users, self._clock, requester)

        match owner:
            case GuestOwner():
                cv = await self._cvs.get(base_cv_id)

                # Authorization is one value equality (ADR-0022): a user-owned id on this guest
                # route is "not mine" exactly as another session's is (AC-8).
                if cv.owner != owner:
                    # "Not mine" must look identical to "does not exist" at this boundary
                    # (F-20/AC-8, ADR-0008): the public exception is `BaseCvNotFound`, same as a
                    # missing id, and the distinction survives only on `__cause__` for this use
                    # case's own tests.
                    raise BaseCvNotFound(str(base_cv_id)) from BaseCvNotOwnedBySession(
                        str(base_cv_id)
                    )

                return cv
            case UserOwner(user_id=user_id):
                # 2.2's rule, reused rather than restated: the user arm's cause is
                # `BaseCvNotOwnedByUser`, whoever owns the row — a guest included.
                return await get_owned_saved_base_cv(self._cvs, base_cv_id, user_id)
            case _:
                assert_never(owner)
