"""The `SummarizeStorage` use case: how the uploads volume splits between guests, users and
nobody (slice 4.4, AC-7, ADR-0035, technical plan §0.5).

**A walk plus one lookup, never a read**: one `scan_older_than` over the whole volume (a cutoff in
the future, so nothing is too young to count) and **one** `owners_of` over the recognised,
non-partial keys; then pure aggregation. Sizes come from `ScannedFile.size_bytes`, which the walk
already holds. An `owners_of` failure escapes: no half-attributed overview. Read-only; unlinks
nothing.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.domain.retention.ports import ExpiredGuestDataPort, OrphanFileScannerPort
from tailorcraft.domain.retention.storage import FileOwnerKind, StorageBucket, StorageOverview
from tailorcraft.domain.shared.clock import Clock

# A cutoff a day ahead of now, so nothing on the volume is too young to count (AC-7 as amended):
# a cutoff of `now` would drop a file whose mtime is a fraction of a second ahead of the
# whole-second `Clock`.
_LOOKAHEAD = timedelta(days=1)


class SummarizeStorage:
    """Flow of `__call__`: `now = clock.now()` → `scanned = await scanner.scan_older_than(cutoff)`
    → `owners = await data.owners_of(recognised non-partial refs)` → bucket each file: `ref is
    None` → unrecognized; `is_partial` → partial; owner `GUEST` / `USER` → guest / user; absent →
    unreferenced → `StorageOverview(…, scanned_at=now)`."""

    def __init__(
        self, scanner: OrphanFileScannerPort, data: ExpiredGuestDataPort, clock: Clock
    ) -> None:
        self._scanner = scanner
        self._data = data
        self._clock = clock

    async def __call__(self) -> StorageOverview:
        now = self._clock.now()
        scanned = await self._scanner.scan_older_than(now + _LOOKAHEAD)
        refs = [f.ref for f in scanned if f.ref is not None and not f.is_partial]
        owners = await self._data.owners_of(refs)

        empty = StorageBucket(files=0, bytes=0)
        guest = user = unreferenced = partial = unrecognized = empty
        for f in scanned:
            one = StorageBucket(files=1, bytes=f.size_bytes)
            if f.ref is None:
                unrecognized += one
            elif f.is_partial:
                partial += one
            elif (owner := owners.get(f.ref)) is FileOwnerKind.GUEST:
                guest += one
            elif owner is FileOwnerKind.USER:
                user += one
            else:
                unreferenced += one
        return StorageOverview(
            guest=guest,
            user=user,
            unreferenced=unreferenced,
            partial=partial,
            unrecognized=unrecognized,
            scanned_at=now,
        )
