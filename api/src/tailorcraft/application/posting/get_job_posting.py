"""The `GetJobPosting` use case: read one `JobPosting`, authorized by the link to its owner.

Renamed from 1.2's `GetJobPosting` in slice 2.3 (§0.3): it takes a `requester: Owner`,
so one use case serves the guest workspace and a signed-in user's.

A use case rather than `postings.get(id)` called straight from a router, **because it carries the
authorization rule** (ADR-0008, ADR-0010):

    What authorizes access to a job posting is **the link** — `posting.owner == GuestOwner(the
    resolved session id)` — checked here, on every read. Owning a session id is not authority over an
    object that references it: a guest session is not a login, and the id itself proves nothing
    about which rows it may see.

The check lives here rather than in a router so that a **second entry point** cannot reach a
`JobPosting` without it. Slice 1.3 reaches one to build a tailoring prompt and 1.5 reaches one to
render an export; both get this rule for free. A check duplicated in every caller is a check one
caller eventually forgets.
"""

from __future__ import annotations

from typing import assert_never

from tailorcraft.application.identity.resolve_owner import resolve_owner
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.ports import GuestSessionRepository, UserRepository
from tailorcraft.domain.posting.errors import (
    JobPostingNotFound,
    JobPostingNotOwnedBySession,
    JobPostingNotOwnedByUser,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.clock import Clock


class GetJobPosting:
    """Look up a `JobPosting` by id, but only if it belongs to `requester`.

    Guest arm: 1.2's rule, below. User arm: the same equality with `JobPostingNotOwnedByUser` as
    the `__cause__`; raises `UserNotFound` if the account is gone.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth `GetBaseCv` applies, repeated here so this use case is safe
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
        users: UserRepository,
        clock: Clock,
    ) -> None:
        self._postings = postings
        self._sessions = sessions
        self._users = users
        self._clock = clock

    async def __call__(self, job_posting_id: JobPostingId, requester: Owner) -> JobPosting:
        owner = await resolve_owner(self._sessions, self._users, self._clock, requester)

        posting = await self._postings.get(job_posting_id)

        # Authorization is one value equality (ADR-0022), for either variant: a user-owned id on a
        # guest's request is "not mine" exactly as another session's is, and the reverse.
        if posting.owner != owner:
            # "Not mine" must be indistinguishable from "does not exist" at this boundary
            # (P-30/AC-14, ADR-0008): the public exception is `JobPostingNotFound`, the same type a
            # missing id raises, because a 403 here would confirm to an attacker that the id exists.
            # The distinction survives only on `__cause__`, which names the requester's path and
            # which this use case's own tests can see and nothing that crosses the wire can.
            cause: JobPostingNotOwnedBySession | JobPostingNotOwnedByUser
            match owner:
                case GuestOwner():
                    cause = JobPostingNotOwnedBySession(str(job_posting_id))
                case UserOwner():
                    cause = JobPostingNotOwnedByUser(str(job_posting_id))
                case _:
                    assert_never(owner)
            raise JobPostingNotFound(str(job_posting_id)) from cause

        return posting
