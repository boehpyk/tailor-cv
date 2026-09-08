"""The `ListBaseCvsForSession` use case: every `BaseCv` a guest session owns.

A use case rather than a bare `cvs.list_for_session(sid)` call for the same reason
`GetBaseCvForSession` is one: it carries the authorization rule, and that rule must not live in a
router (technical-plan.md, "Use cases"). Here the rule is enforced by construction rather than by a
per-row comparison — **the link** (`cv.guest_session_id == the resolved session id`) is exactly what
`list_for_session` queries by, so there is no row in the result a caller does not own. Owning a
session id is not authority over an object that references it (ADR-0008); the corollary for a list
endpoint is that the query itself must never be parameterized by anything the caller supplies other
than its own resolved session id.

Centralizing this here, rather than trusting every future caller to filter correctly, is what keeps
a second entry point (the Celery task 1.5 adds) from being able to list another session's CVs by
skipping a check that only exists in a router.
"""

from __future__ import annotations

from collections.abc import Sequence

from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.shared.clock import Clock


class ListBaseCvsForSession:
    """Every `BaseCv` owned by `guest_session_id`, or an empty sequence — never a 404 (AC-9's GET
    contract: an empty list is a perfectly ordinary answer to "what does this session own").

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves,
    the same defense-in-depth `GetBaseCvForSession` applies, so this use case is self-contained
    against a caller that skips the API's own cookie dependency.
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

    async def __call__(self, guest_session_id: GuestSessionId) -> Sequence[BaseCv]:
        raise NotImplementedError
