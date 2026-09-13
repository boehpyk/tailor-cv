"""The `GetTailoringRunForSession` use case: read one `TailoringRun`, authorized by the link to its
session.

A use case rather than `runs.get(id)` called straight from a router, **because it carries the
authorization rule** (ADR-0008, ADR-0010):

    What authorizes access to a tailoring run is **the link** — `run.guest_session_id == the
    resolved session id` — checked here, on every read. Owning a session id is not authority over an
    object that references it: a guest session is not a login, and the id itself proves nothing
    about which rows it may see.

The check lives here rather than in a router so that a **second entry point** cannot reach a
`TailoringRun` without it. This slice already has two — the HTTP polling endpoint and, in 1.5, the
export path — and a check duplicated in every caller is a check one caller eventually forgets.

The session is resolved by the shared `resolve_active_guest_session`
(`application/identity/resolve_guest_session.py`), the same helper the `intake` and `posting` read
use cases call: session resolution is not re-implemented per context, so an expiry rule that changes
changes in one place.
"""

from __future__ import annotations

from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.tailoring.errors import (
    TailoringRunNotFound,
    TailoringRunNotOwnedBySession,
)
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId


class GetTailoringRunForSession:
    """Look up a `TailoringRun` by id, but only if it belongs to `guest_session_id`.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth `GetBaseCvForSession` and `GetJobPostingForSession` apply, repeated
    here so this use case is safe to call from anywhere and not only from behind the API's cookie
    dependency.

    Raises `TailoringRunNotFound` in **two** situations that must be indistinguishable from outside:
    the id does not exist at all, and the id exists but names a run owned by a *different* session
    (G-29/AC-14). This use case never raises `TailoringRunNotOwnedBySession` to its caller and the
    API never maps a 403 — a distinguishable "wrong owner" response would confirm to someone
    guessing ids that the id exists, which is exactly what a 404 is supposed to withhold. That
    matters more here than for a CV or a posting: a run id is the polling handle, so it is the id an
    attacker is most likely to be enumerating.

    `TailoringRunNotOwnedBySession` still exists as a type, and T16 is expected to raise
    ``TailoringRunNotFound(...) from TailoringRunNotOwnedBySession(...)``: this use case's own tests
    need to tell "absent" from "not mine" apart even though the boundary must not, and `__cause__`
    is where that distinction survives without ever crossing the wire.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        sessions: GuestSessionRepository,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._clock = clock

    async def __call__(
        self, run_id: TailoringRunId, guest_session_id: GuestSessionId
    ) -> TailoringRun:
        session = await resolve_active_guest_session(self._sessions, self._clock, guest_session_id)

        run = await self._runs.get(run_id)

        if run.guest_session_id != session.id:
            # "Not mine" must be indistinguishable from "does not exist" at this boundary
            # (G-29/AC-14, ADR-0008): the public exception is `TailoringRunNotFound`, the same type
            # `runs.get` raises for an id that was never issued, because a 403 here would confirm to
            # someone enumerating polling handles that the id is real. The distinction survives only
            # on `__cause__`, where this use case's own tests can see it and nothing that crosses the
            # wire can.
            raise TailoringRunNotFound(str(run_id)) from TailoringRunNotOwnedBySession(str(run_id))

        return run
