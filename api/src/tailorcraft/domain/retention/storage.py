"""How the uploads volume splits, in counts and bytes (slice 4.4, ADR-0035, technical plan §0.5).

The overview is a walk plus one lookup, never a read of a file: the scanner reports every entry with
its size, `ExpiredGuestDataPort.owners_of` says which keys a guest's row or a user's row names, and
`SummarizeStorage` adds them up into five buckets. No key, path or filename leaves this shape —
only counts — so the overview cannot leak what is on the volume (Constitution §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tailorcraft.domain.shared.errors import InvariantViolated


class FileOwnerKind(StrEnum):
    """Whose row names a stored key. Exactly one, by ADR-0022's `ck_*_exactly_one_owner`; a key no
    row names has no kind and is simply absent from `owners_of`'s answer."""

    GUEST = "guest"
    USER = "user"


@dataclass(frozen=True, slots=True)
class StorageBucket:
    """A number of files and their total size in bytes, both `>= 0`. Buckets add."""

    files: int
    bytes: int

    def __post_init__(self) -> None:
        if self.files < 0 or self.bytes < 0:
            raise InvariantViolated("a storage bucket's files and bytes must be >= 0")

    def __add__(self, other: StorageBucket) -> StorageBucket:
        return StorageBucket(files=self.files + other.files, bytes=self.bytes + other.bytes)


@dataclass(frozen=True, slots=True)
class StorageOverview:
    """The volume at `scanned_at`, in five disjoint buckets.

    `guest` and `user` are recognised keys a row names; `unreferenced` are recognised keys no row
    names (the orphan sweep's candidates, of any age); `partial` are `.part` files; `unrecognized`
    are names that are not a `FileRef` at all — counted, never named.
    """

    guest: StorageBucket
    user: StorageBucket
    unreferenced: StorageBucket
    partial: StorageBucket
    unrecognized: StorageBucket
    scanned_at: datetime

    def __post_init__(self) -> None:
        if self.scanned_at.utcoffset() is None:
            raise InvariantViolated("scanned_at must be timezone-aware")
