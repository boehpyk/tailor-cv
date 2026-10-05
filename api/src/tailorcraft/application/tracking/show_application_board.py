"""The `ShowApplicationBoard` use case: a signed-in user's whole board (slice 3.1, technical plan §2,
AC-12).

**SKELETON (T10).** The constructor is real; `__call__` raises `NotImplementedError` until T12.

Flow: ``resolve_existing_user`` (`UserNotFound`) → ``board.board_for_user(user_id)``, returned
unchanged. A read model behind a query port (ADR-0024): no aggregate is loaded, no document is
selected, and the order is the query's (most recently moved first, AC-30).

**No command dataclass**: one verified id, the `EraseAccount` precedent for a one-value input.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.board import ApplicationBoard
from tailorcraft.domain.tracking.ports import ApplicationBoardQuery


class ShowApplicationBoard:
    """Return `user_id`'s board (module docstring)."""

    def __init__(self, users: UserRepository, board: ApplicationBoardQuery) -> None:
        self._users = users
        self._board = board

    async def __call__(self, user_id: UserId) -> ApplicationBoard:
        raise NotImplementedError


__all__ = ["ShowApplicationBoard"]
