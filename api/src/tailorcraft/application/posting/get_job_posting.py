"""The `GetJobPostingForSession` use case: read one `JobPosting`, authorized by the link to its
session.

**SKELETON (T11).** `__call__` raises `NotImplementedError`; the body arrives at T13.

A use case rather than `postings.get(id)` called straight from a router, **because it carries the
authorization rule** (ADR-0008, ADR-0010):

    What authorizes access to a job posting is **the link** — `posting.guest_session_id == the
    resolved session id` — checked here, on every read. Owning a session id is not authority over an
    object that references it: a guest session is not a login, and the id itself proves nothing
    about which rows it may see.

The check lives here rather than in a router so that a **second entry point** cannot reach a
`JobPosting` without it. Slice 1.3 reaches one to build a tailoring prompt and 1.5 reaches one to
render an export; both get this rule for free. A check duplicated in every caller is a check one
caller eventually forgets.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.clock import Clock


class GetJobPostingForSession:
    """Look up a `JobPosting` by id, but only if it belongs to `guest_session_id`.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth `GetBaseCvForSession` applies, repeated here so this use case is safe
    to call from anywhere and not only from behind the API's cookie dependency.

    Raises `JobPostingNotFound` in **two** situations that must be indistinguishable from outside:
    the id does not exist at all, and the id exists but names a posting owned by a *different*
    session (P-30/AC-14). This use case never raises `JobPostingNotOwnedBySession` to its caller and
    the API never maps a 403 — a distinguishable "wrong owner" response would confirm to an attacker
    that the id exists, which is exactly what a 404 is supposed to withhold.

    `JobPostingNotOwnedBySession` still exists as a type, and T13 is expected to raise
    ``JobPostingNotFound(...) from JobPostingNotOwnedBySession(...)``: this use case's own tests need
    to tell "absent" from "not mine" apart even though the boundary must not, and `__cause__` is
    where that distinction survives without ever crossing the wire.
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

    async def __call__(
        self, job_posting_id: JobPostingId, guest_session_id: GuestSessionId
    ) -> JobPosting:
        raise NotImplementedError
