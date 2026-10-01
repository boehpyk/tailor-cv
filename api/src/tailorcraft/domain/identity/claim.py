"""What a claim of guest work moved, and what it reports (ADR-0025 decision 8).

A **claim** is the hand-off ADR-0010 kept apart: a signed-in user takes the work a guest session in
the same browser made, every row of it at once. It lives in `identity` because it is the hand-off
between the two principals, and the `Owner` type it rewrites lives in `ownership.py` beside it.
`retention` was considered and rejected: retention *deletes* what an owner has; a claim *keeps* it.

**No aggregate.** An aggregate protects an invariant its own methods can enforce. The claim's
invariant is *every row of a session changes owner together*, and it spans four contexts' tables
(`intake`, `posting`, `tailoring`, `export`). Nothing in this process can hold that. **One transaction
in one adapter** does, behind `GuestWorkClaimPort`, as ADR-0018 argued for the purge. The one domain
rule the claim applies, `GuestSession.is_expired`, stays on `GuestSession`, where it is applied
rather than restated.

**No domain event.** Its only listener would be the logger that the entry point already is (ADR-0018
decision 9's reasoning). A `GuestWorkClaimed(guest_session_id, user_id)` payload would also be the one
durable record pairing a browser session with an account, which is the pairing the router is careful
never to log. The report is **returned**, and the router logs its counts.

So this module holds two value objects and nothing else:

- `ClaimedGuestWork` is what `GuestWorkClaimPort.transfer` hands back: the row counts it re-keyed,
  the working copies it dropped, and the dropped copies' file keys (from `RETURNING`), still to be
  unlinked. The rows are durable when it is returned; the files are not yet gone.
- `GuestWorkClaimReport` is what the use case returns once it has tried those unlinks.

**Why `unlink_failures` is `tuple[str, ...]` and not retention's `FileUnlinkFailure`.** The shape is
the same (an exception's type name, never its message, never the key), but reusing that type would
make `identity` import `retention`, and a domain module does not import a sibling context even where
the layers allow it. The use case builds each name with the same one-line rule
(`type(exc).__name__`) as `FileUnlinkFailure.from_exception`. They share a shape, not a type.

Nothing here names another context's aggregate: counts and `FileRef`s (`domain/shared`) only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tailorcraft.domain.shared.files import FileRef


@dataclass(frozen=True, slots=True)
class ClaimedGuestWork:
    """What `GuestWorkClaimPort.transfer` moved and dropped; the rows are durable on return.

    `files_to_unlink` may be shorter than `working_copies_dropped` only if a dropped row had no file
    key, which `intake_base_cv` forbids. It may never be longer: a key with no dropped row behind it
    would be a file this claim had no authority to unlink. The inequality is the invariant.
    """

    base_cvs: int
    job_postings: int
    tailoring_runs: int
    export_jobs: int
    working_copies_dropped: int
    files_to_unlink: tuple[FileRef, ...]

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class GuestWorkClaimReport:
    """The outcome of one claim: what changed owner, what was dropped, and which unlinks failed.

    `unlink_failures` holds exception type names. They are **returned, never logged here**, because
    `application/` and `domain/` stay silent and the entry point decides what reaches a log.
    """

    base_cvs: int
    job_postings: int
    tailoring_runs: int
    export_jobs: int
    working_copies_dropped: int
    files_unlinked: int
    unlink_failures: tuple[str, ...]

    def __post_init__(self) -> None:
        raise NotImplementedError

    @classmethod
    def nothing(cls) -> GuestWorkClaimReport:
        """The report of a claim with no guest session to take from: every count zero."""
        raise NotImplementedError

    @classmethod
    def of(cls, claimed: ClaimedGuestWork, failures: Sequence[str]) -> GuestWorkClaimReport:
        """The report of a transfer whose file unlinks were tried, `failures` being their type names."""
        raise NotImplementedError

    @property
    def claimed_anything(self) -> bool:
        """Whether any of the four row counts is above zero (dropped working copies do not count)."""
        raise NotImplementedError
