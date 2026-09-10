"""The `ListJobPostingsForSession` use case: every `JobPosting` a guest session owns.

A use case rather than a bare `postings.list_for_session(sid)` for the same reason
`GetJobPostingForSession` is one: it carries the authorization rule. Here the rule is enforced **by
construction** rather than by a per-row comparison — the link (`posting.guest_session_id == the
resolved session id`) is exactly what `list_for_session` queries by, so there is no row in the
result the caller does not own.

The corollary for a list endpoint is worth stating on its own, because it is the mistake this shape
prevents: **the query must never be parameterized by anything the caller supplies other than its own
resolved session id.** A `?session_id=` parameter would turn this into an enumeration endpoint, and
it would look perfectly reasonable in a router.
"""

from __future__ import annotations

from collections.abc import Sequence

from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.shared.clock import Clock


class ListJobPostingsForSession:
    """Every `JobPosting` owned by `guest_session_id`, or an empty sequence — never a 404.

    "This session owns nothing yet" is an ordinary answer to "what does this session own", not an
    error, and the API contract says the same: `items: []`, status 200.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth `GetJobPostingForSession` applies, so this use case is self-contained
    against a caller that skips the API's own cookie dependency.
    """

    def __init__(
        self,
        postings: JobPostingRepository,
        sessions: GuestSessionRepository,
        clock: Clock,
    ) -> None:
        self._postings = postings
        self._sessions = sessions
        self._clock = clock

    async def __call__(self, guest_session_id: GuestSessionId) -> Sequence[JobPosting]:
        session = await resolve_active_guest_session(self._sessions, self._clock, guest_session_id)
        # Note what is passed: `session.id`, the id this use case just resolved — never an id the
        # caller supplied. That is the whole authorization rule for a list endpoint, and it is
        # enforced by there being nothing else available to pass.
        return await self._postings.list_for_session(session.id)
