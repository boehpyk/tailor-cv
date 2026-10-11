"""The `ListUsers` use case: one keyset page of the administrator's user directory (slice 4.2,
AC-10, ADR-0033). A pass-through to `UserDirectoryQuery`, so the router depends on a use case like
every other route, and so the page-size bound (1..`PAGE_SIZE_MAX`) is asserted in one place.
Read-only; takes no lock.
"""

from __future__ import annotations

from tailorcraft.domain.identity.user_directory import (
    PAGE_SIZE_MAX,
    EmailSearch,
    UserDirectoryCursor,
    UserDirectoryPage,
    UserDirectoryQuery,
)
from tailorcraft.domain.shared.errors import InvariantViolated


class ListUsers:
    """Up to `limit` accounts matching `search` (all when `None`), newest first, after `after`."""

    def __init__(self, directory: UserDirectoryQuery) -> None:
        self._directory = directory

    async def __call__(
        self, search: EmailSearch | None, after: UserDirectoryCursor | None, limit: int
    ) -> UserDirectoryPage:
        # The boundary already bounds `limit`; reaching here out of range is a caller's bug.
        if not 1 <= limit <= PAGE_SIZE_MAX:
            raise InvariantViolated(f"a directory page holds 1..{PAGE_SIZE_MAX} entries")
        return await self._directory.page(search, after, limit)
