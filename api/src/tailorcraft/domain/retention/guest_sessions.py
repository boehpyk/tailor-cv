"""The administrator's directory of live guest sessions: a read model behind a query port (slice 4.4,
ADR-0035; ADR-0024's shape, 4.2's `user_directory.py` as the sibling).

A directory row is not a `GuestSession`: it is the session's instants plus four counts of what the
session owns, read in one statement — never an aggregate per row, never a CV body, a filename or a
posting's text. Only **live** sessions are listed (`expires_at > as_of`): an expired one is the
purge's business, and the admin sees it as "gone".

Keyset pages on `(expires_at, session_id)` descending, never `OFFSET`. The cursor is an unsigned
domain value; the boundary encodes it and raises `InvalidGuestSessionCursor` on a bad one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.errors import InvalidGuestSessionCursor
from tailorcraft.domain.shared.errors import InvariantViolated

PAGE_SIZE_MAX = 50


@dataclass(frozen=True, slots=True)
class GuestSessionSummary:
    """One live guest session as the directory shows it: its instants and what it owns, in counts.

    Counts are `>= 0`; both instants are timezone-aware and `expires_at > created_at`. No
    cookie value, no file key, nothing a stranger uploaded — the session id is the only handle.
    """

    session_id: GuestSessionId
    created_at: datetime
    expires_at: datetime
    base_cvs: int
    job_postings: int
    tailoring_runs: int
    export_jobs: int

    def __post_init__(self) -> None:
        for name in ("base_cvs", "job_postings", "tailoring_runs", "export_jobs"):
            if getattr(self, name) < 0:
                raise InvariantViolated(f"{name} must be >= 0")
        # `utcoffset() is None`, not `tzinfo is None`: a tzinfo may decline to give an offset.
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise InvariantViolated("created_at and expires_at must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise InvariantViolated("expires_at must be after created_at")


@dataclass(frozen=True, slots=True)
class GuestSessionCursor:
    """Where the next page starts: the last entry's `(expires_at, session_id)`.

    `expires_at` must be timezone-aware and whole-second (the `Clock` contract, ADR-0007) → otherwise `InvalidGuestSessionCursor`. Shares
    `UserDirectoryCursor`'s shape and none of its code: two value objects with the same shape get no
    base class (CLAUDE.md).
    """

    expires_at: datetime
    session_id: GuestSessionId

    def __post_init__(self) -> None:
        if self.expires_at.utcoffset() is None:
            raise InvalidGuestSessionCursor("expires_at must be timezone-aware")
        if self.expires_at.microsecond != 0:
            raise InvalidGuestSessionCursor("expires_at must be whole-second")


@dataclass(frozen=True, slots=True)
class GuestSessionPage:
    """Up to one page of summaries, latest `expires_at` first; `next_cursor` is `None` exactly when
    no more exist."""

    items: tuple[GuestSessionSummary, ...]
    next_cursor: GuestSessionCursor | None


class GuestSessionDirectoryQuery(Protocol):
    """Every live guest session, one keyset page at a time — a read-side port, not a repository.

    **Not a method on identity's `GuestSessionRepository`**, for `ExpiredGuestDataPort`'s reason:
    that port resolves one cookie at a time in the language of authentication; "list them with what
    each owns" is retention's question (ADR-0035).
    """

    async def page(
        self, as_of: datetime, after: GuestSessionCursor | None, limit: int
    ) -> GuestSessionPage:
        """Up to `limit` sessions live at `as_of` (`expires_at > as_of`), in `(expires_at,
        session_id)` descending order, strictly after `after`."""
        ...

    async def get(self, session_id: GuestSessionId, as_of: datetime) -> GuestSessionSummary | None:
        """That session's summary if it exists and is live at `as_of`; `None` otherwise — gone and
        expired are one answer to an admin."""
        ...
