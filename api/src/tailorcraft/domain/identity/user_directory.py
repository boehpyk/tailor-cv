"""The administrator's user directory: a read model behind a query port (slice 4.2, ADR-0033;
ADR-0024's shape).

A directory row is not a `User`: it never carries a password hash, and loading an aggregate per row
to show a list is the cost 2.2's T30 measured. Keyset pages on `(created_at, user_id)`, never
`OFFSET`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tailorcraft.domain.identity.value_objects import EmailAddress, Role, UserId

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 50


@dataclass(frozen=True, slots=True)
class EmailSearch:
    """A substring of an email to filter the directory by: stripped, lower-cased (`EmailAddress`'s
    normalization), 1..254 characters, no control characters → otherwise `InvalidEmailSearch`.

    **No pattern semantics.** `%` and `_` are literal characters: the adapter matches with
    `strpos`, never `LIKE`, so a search cannot widen itself into "every row".
    """

    value: str

    def __post_init__(self) -> None:
        pass  # skeleton: no rule yet, so a "refused" RED fails on DID NOT RAISE

    @classmethod
    def parse(cls, raw: str) -> EmailSearch | None:
        """`None` for an empty or whitespace-only `raw` (no filter); otherwise the normalized search."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class UserDirectoryCursor:
    """Where the next page starts: the last entry's `(created_at, user_id)`.

    `created_at` must be timezone-aware and whole-second (the `Clock` contract, ADR-0007) →
    otherwise `InvalidUserDirectoryCursor`. Shares `HistoryCursor`'s shape and none of its code:
    two value objects with the same shape get no base class (CLAUDE.md).
    """

    created_at: datetime
    user_id: UserId

    def __post_init__(self) -> None:
        pass  # skeleton: no rule yet, so a "refused" RED fails on DID NOT RAISE


@dataclass(frozen=True, slots=True)
class UserDirectoryEntry:
    """One account as the directory shows it. No password hash, no counts."""

    id: UserId
    email: EmailAddress
    role: Role
    created_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True, slots=True)
class UserDirectoryPage:
    """Up to one page of entries, newest first; `next_cursor` is `None` exactly when no more exist."""

    entries: tuple[UserDirectoryEntry, ...]
    next_cursor: UserDirectoryCursor | None


class UserDirectoryQuery(Protocol):
    """Every registered account, one keyset page at a time — a read-side port, not a repository."""

    async def page(
        self, search: EmailSearch | None, after: UserDirectoryCursor | None, limit: int
    ) -> UserDirectoryPage:
        """Up to `limit` accounts whose email contains `search` (all when `None`), newest first,
        strictly after `after` in `(created_at, user_id)` descending order."""
        ...
