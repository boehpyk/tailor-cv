"""The `ListTailoringHistory` use case: one page of a signed-in user's tailoring history (slice 2.3).

Resolves the user, then asks the read-side port for one keyset page (ADR-0024). Authorized **by
construction**, like the guest lists: the query takes the resolved user's id and nothing else a
caller supplies, and the cursor is an echo of a value this server handed out, so a forged one can
only move a user around their own history.

No aggregate is loaded and no document body is read: an entry says a run exists and how it ended,
and the documents are one click away on the run's own endpoint.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.history import HistoryCursor, HistoryPage, HistoryPageSize
from tailorcraft.domain.tailoring.ports import TailoringHistoryQuery


class ListTailoringHistory:
    """`user_id`'s runs, newest first, one page of `size` strictly after `after` (from the start
    when `None`).

    Raises `UserNotFound` if the account is gone (a still-valid access token for an erased
    account), via `resolve_existing_user`. An empty page for a user with no runs — never an error.
    """

    def __init__(self, history: TailoringHistoryQuery, users: UserRepository) -> None:
        self._history = history
        self._users = users

    async def __call__(
        self, user_id: UserId, after: HistoryCursor | None, size: HistoryPageSize
    ) -> HistoryPage:
        raise NotImplementedError


__all__ = ["ListTailoringHistory"]
