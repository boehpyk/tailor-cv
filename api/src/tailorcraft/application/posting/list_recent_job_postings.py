"""The `ListRecentJobPostingsForUser` use case: a signed-in user's most recent job postings.

The account twin of `ListJobPostingsForSession`, for the signed-in workspace's "recent postings"
list (slice 2.3, §2). Authorized **by construction**, like its guest sibling: the query takes the
resolved user's id and nothing else a caller supplies, so there is no row in the result the caller
does not own.

Bounded rather than paged: a user may keep up to `max_job_postings_per_user` postings, and the
workspace shows only the newest few. `limit` is the caller's (the router keeps it ≤ 20); the history
page is where older work is found.
"""

from __future__ import annotations

from collections.abc import Sequence

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingRepository


class ListRecentJobPostingsForUser:
    """`user_id`'s newest postings, newest first, at most `limit` — or an empty sequence.

    Raises `UserNotFound` if the account is gone (a still-valid access token for an erased
    account), via `resolve_existing_user`.
    """

    def __init__(self, postings: JobPostingRepository, users: UserRepository) -> None:
        self._postings = postings
        self._users = users

    async def __call__(self, user_id: UserId, limit: int) -> Sequence[JobPosting]:
        raise NotImplementedError


__all__ = ["ListRecentJobPostingsForUser"]
