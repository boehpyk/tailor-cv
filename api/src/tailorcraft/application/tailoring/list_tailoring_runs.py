"""The `ListTailoringRunsForSession` use case: every `TailoringRun` a guest session owns.

A use case rather than a bare `runs.list_for_session(sid)` for the same reason
`GetTailoringRunForSession` is one: it carries the authorization rule. Here the rule is enforced
**by construction** rather than by a per-row comparison — the link (`run.guest_session_id == the
resolved session id`) is exactly what `list_for_session` queries by, so there is no row in the
result the caller does not own.

The corollary for a list endpoint is worth stating on its own, because it is the mistake this shape
prevents: **the query must never be parameterized by anything the caller supplies other than its own
resolved session id.** A `?session_id=` parameter would turn this into an enumeration endpoint, and
it would look perfectly reasonable in a router.

The session is resolved by the shared `resolve_active_guest_session`
(`application/identity/resolve_guest_session.py`), the same helper every other read use case calls,
rather than each context re-resolving a session its own way.
"""

from __future__ import annotations

from collections.abc import Sequence

from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun


class ListTailoringRunsForSession:
    """Every `TailoringRun` owned by `guest_session_id`, newest first, or an empty sequence.

    Never a 404: "this session has not tailored anything yet" is an ordinary answer to "what does
    this session own", not an error, and the API contract says the same — `items: []`, status 200.

    **Newest first is the repository's job**, not a sort here: `list_for_session` orders on
    `requested_at DESC, id DESC` in SQL. Re-sorting in Python would mean loading every document body
    to order rows the database can order for free, and would quietly become the real ordering the
    day the two disagreed — one ordering, in one place, and `id DESC` breaks the tie between two
    runs requested in the same whole second (the `Clock` port is whole-second by contract).

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth `GetTailoringRunForSession` applies, so this use case is
    self-contained against a caller that skips the API's own cookie dependency.
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

    async def __call__(self, guest_session_id: GuestSessionId) -> Sequence[TailoringRun]:
        session = await resolve_active_guest_session(self._sessions, self._clock, guest_session_id)
        # Note what is passed: `session.id`, the id this use case just resolved — never an id the
        # caller supplied. That is the whole authorization rule for a list endpoint, and it is
        # enforced by there being nothing else available to pass. The ordering
        # (`requested_at DESC, id DESC`) is the repository's, deliberately not re-applied here.
        return await self._runs.list_for_session(session.id)
